#! /usr/bin/env python
# -*- coding: utf-8 -*-

import argparse

parser = argparse.ArgumentParser(description="Convert bag files to TensorFlow TFRecords.",
                                 formatter_class=argparse.RawTextHelpFormatter)

parser.add_argument("bag_files", metavar="F", type=str, nargs='+',
                    help="The bag file(s) to convert. Supports \"path/to/*.bag\"")

parser.add_argument("--output_dir", metavar="OUT", dest="output_dir", type=str,
                    help="Output directory (default: {bag_file}/TFRecord)")

parser.add_argument("--image_topic", metavar="IM", dest="image_topic", type=str,
                    default="/camera/color/image_raw",
                    help="Image topic (default: \"/camera/color/image_raw\")")

parser.add_argument("--images_per_bin", metavar="MAX", type=int,
                    default=10000,
                    help="Maximum number of images per TFRecord file (default: 10000)")

parser.add_argument("--image_crop", metavar=("X", "Y", "W", "H"), type=int, nargs=4,
                    help="Crop images using. Cropping is applied before scaling (default: Complete image)")

parser.add_argument("--image_scale", metavar="SCALE", type=float,
                    default=1.0,
                    help="Scale images by this factor (default: 1.0)")

parser.add_argument("--tf_map", metavar="TF_M", dest="tf_map", type=str,
                    default="map",
                    help="TF reference frame (default: map)")

parser.add_argument("--tf_base_link", metavar="TF_B", dest="tf_base_link", type=str,
                    default="realsense_link",
                    help="TF camera frame (default: base_link)")

parser.add_argument("--label", metavar="L", dest="label", type=int,
                    default=0,
                    help=" 0: Unknown (default)\n"
                         " 1: No anomaly\n"
                         " 2: Contains an anomaly")

args = parser.parse_args()

import os
import sys
import time
from glob import glob

from common import Visualize, utils, logger

import rospy
import rosbag
import tensorflow as tf
import cv2
from cv_bridge import CvBridge, CvBridgeError
import tf as ros_tf
import tf2_ros
import tf2_py as tf2
import numpy as np
from tqdm import tqdm

def _int64_feature(value):
    """Wrapper for inserting int64 features into Example proto."""
    if not isinstance(value, list):
        value = [value]
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))

def _float_feature(value):
    """Wrapper for inserting float features into Example proto."""
    if not isinstance(value, list):
        value = [value]
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))

# Can be used to store float64 values if necessary
# (http://jrmeyer.github.io/machinelearning/2019/05/29/tensorflow-dataset-estimator-api.html)
def _float64_feature(float64_value):
    float64_bytes = [str(float64_value).encode()]
    bytes_list = tf.train.BytesList(value=float64_bytes)
    bytes_list_feature = tf.train.Feature(bytes_list=bytes_list)
    return bytes_list_feature
    #    example['float_value'] = tf.strings.to_number(example['float_value'], out_type=tf.float64)

def _bytes_feature(value):
    """Wrapper for inserting bytes features into Example proto."""
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

def rosbag_to_tfrecord():
    ################
    #  Parameters  #
    ################
    bag_files      = args.bag_files
    output_dir     = args.output_dir
    image_topic    = args.image_topic
    images_per_bin = args.images_per_bin
    tf_map         = args.tf_map
    tf_base_link   = args.tf_base_link
    label          = args.label

    # Check parameters
    if not bag_files or len(bag_files) < 1 or bag_files[0] == "":
        raise ValueError("Please specify at least one filename (%s)" % bag_files)
    
    # Expand wildcards
    bag_files_expanded = []
    for s in bag_files:
        bag_files_expanded += glob(s)
    bag_files = list(set(bag_files_expanded)) # Remove duplicates

    if output_dir is None or output_dir == "" or not os.path.exists(output_dir) or not os.path.isdir(output_dir):
        output_dir = os.path.join(os.path.abspath(os.path.dirname(bag_files[0])), "TFRecord")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        logger.info("Output directory set to %s" % output_dir)

    if image_topic is None or image_topic == "":
        logger.error("No image topic given. Use parameter image_topic.")
        return

    if tf_map is None or tf_map == "" or tf_base_link == "":
        logger.error("Please specify tf frame names.")
        return

    if images_per_bin is None or images_per_bin < 1:
        logger.error("images_per_bin has to be greater than 1.")
        return

    if 0 > label or label > 2:
        logger.error("label has to be between 0 and 2.")
        return

    # Add progress bar if multiple files
    if len(bag_files) > 1:
        bag_files = tqdm(bag_files, desc="Bag files", file=sys.stderr)

    for bag_file in bag_files:
        # Check parameters
        if bag_file == "" or not os.path.exists(bag_file) or not os.path.isfile(bag_file):
            logger.error("Specified bag does not exist (%s)" % bag_file)
            continue

        logger.info("Extracting %s" % bag_file)

        bag_file_name = os.path.splitext(os.path.basename(bag_file))[0]

        # Used to convert image message to opencv image
        bridge = CvBridge()
        
        ################
        #     MAIN     #
        ################
        with rosbag.Bag(bag_file, "r") as bag:
            ### Get /tf transforms
            expected_tf_count = bag.get_message_count(["/tf", "/tf_static"])
            
            tf_buffer = tf2_ros.Buffer(cache_time=rospy.Duration(bag.get_end_time() - bag.get_start_time()), debug=False)
                
            for topic, msg, t in tqdm(bag.read_messages(topics=["/tf", "/tf_static"]),
                                        desc="Extracting transforms",
                                        total=expected_tf_count,
                                        file=sys.stderr):
                for msg_tf in msg.transforms:
                    if topic == "/tf_static":
                        tf_buffer.set_transform_static(msg_tf, "default_authority")
                    else:
                        tf_buffer.set_transform(msg_tf, "default_authority")

            ### Get images
            expected_im_count = bag.get_message_count(image_topic)
            number_of_bins = expected_im_count // images_per_bin + 1

            total_saved_count = 0
            per_bin_count = 0
            skipped_count = 0
            tfWriter = None        

            colorspace = b"RGB"
            channels = 3

            start = time.time()

            with tqdm(desc="Writing TFRecord",
                        total=expected_im_count,
                        file=sys.stderr) as pbar:
                for topic, msg, t in bag.read_messages(topics=image_topic):
                    try:
                        # Get translation and orientation
                        msg_tf = tf_buffer.lookup_transform(tf_map, tf_base_link, t)#, rospy.Duration.from_sec(0.001))
                        translation = msg_tf.transform.translation
                        euler = ros_tf.transformations.euler_from_quaternion([msg_tf.transform.rotation.x, msg_tf.transform.rotation.y, msg_tf.transform.rotation.z, msg_tf.transform.rotation.w])

                        # Get the image
                        if msg._type == "sensor_msgs/CompressedImage":
                            image_arr = np.fromstring(msg.data, np.uint8)
                            cv_image = cv2.imdecode(image_arr, cv2.IMREAD_COLOR)
                        elif msg._type == "sensor_msgs/Image":
                            cv_image = bridge.imgmsg_to_cv2(msg, "bgr8")
                        else:
                            raise ValueError("Image topic type must be either \"sensor_msgs/Image\" or \"sensor_msgs/CompressedImage\".")
                        
                        # Crop the image
                        if args.image_crop is not None:
                            cv_image = cv_image[args.image_crop[1]:args.image_crop[1] + args.image_crop[3], # y:y+h
                                                args.image_crop[0]:args.image_crop[0] + args.image_crop[2]] # x:x+w

                        # Scale the image
                        if args.image_scale != 1.0:
                            cv_image = cv2.resize(cv_image, (int(cv_image.shape[1] * args.image_scale),
                                                             int(cv_image.shape[0] * args.image_scale)), cv2.INTER_AREA)

                        _, encoded = cv2.imencode(".jpeg", cv_image)

                        # Create a new writer if we need one
                        if not tfWriter or per_bin_count >= images_per_bin:
                            if tfWriter:
                                tfWriter.close()
                            
                            if number_of_bins == 1:
                                output_filename = "%s.tfrecord" % bag_file_name
                            else:
                                bin_number = total_saved_count // images_per_bin + 1
                                output_filename = "%s.%.5d-of-%.5d.tfrecord" % (bag_file_name, bin_number, number_of_bins)
                                
                            output_file = os.path.join(output_dir, output_filename)
                            tfWriter = tf.io.TFRecordWriter(output_file)
                            per_bin_count = 0

                        # Add image and position to TFRecord
                        feature_dict = {
                            "metadata/location/translation/x"   : _float_feature(translation.x),
                            "metadata/location/translation/y"   : _float_feature(translation.y),
                            "metadata/location/translation/z"   : _float_feature(translation.z),
                            "metadata/location/rotation/x"      : _float_feature(euler[0]),
                            "metadata/location/rotation/y"      : _float_feature(euler[1]),
                            "metadata/location/rotation/z"      : _float_feature(euler[2]),
                            "metadata/time"                     : _int64_feature(t.to_nsec()),
                            "metadata/label"                    : _int64_feature(label), # 0: Unknown, 1: No anomaly, 2: Contains an anomaly
                            "metadata/rosbag"                   : _bytes_feature(bag_file),
                            "metadata/tfrecord"                 : _bytes_feature(output_file),
                            "image/height"      : _int64_feature(cv_image.shape[0]),
                            "image/width"       : _int64_feature(cv_image.shape[1]),
                            "image/channels"    : _int64_feature(channels),
                            "image/colorspace"  : _bytes_feature(colorspace),
                            "image/format"      : _bytes_feature("jpeg"),
                            "image/encoded"     : _bytes_feature(encoded.tobytes())
                        }

                        example = tf.train.Example(features=tf.train.Features(feature=feature_dict))
                        
                        tfWriter.write(example.SerializeToString())
                        per_bin_count += 1
                        total_saved_count += 1

                    except KeyboardInterrupt:
                        logger.info("Cancelled")
                        return
                    except tf2.ExtrapolationException:
                        skipped_count += 1

                    # Print progress
                    pbar.set_postfix("%i skipped" % skipped_count)
                    pbar.update()

                if tfWriter:
                    tfWriter.close()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    rosbag_to_tfrecord()