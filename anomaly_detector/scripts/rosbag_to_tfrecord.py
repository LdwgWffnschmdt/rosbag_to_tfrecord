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

parser.add_argument("--tf_map", metavar="TF_M", dest="tf_map", type=str,
                    default="map",
                    help="TF reference frame (default: map)")

parser.add_argument("--tf_base_link", metavar="TF_B", dest="tf_base_link", type=str,
                    default="realsense_link",
                    help="TF camera frame (default: base_link)")

parser.add_argument("--label", metavar="L", dest="label", type=int,
                    default=0,
                    help="-2: labeling mode (show image and wait for input) [space]: No anomaly\n"
                         "                                                  [tab]  : Contains anomaly\n"
                         "-1: continuous labeling mode (show image for 10ms, keep label until change)\n"
                         " 0: Unknown (default)\n"
                         " 1: No anomaly\n"
                         " 2: Contains an anomaly")

args = parser.parse_args()

import os
import time
import logging

import rospy
import rosbag
import tensorflow
import cv2
from cv_bridge import CvBridge, CvBridgeError
import tf
import tf2_ros
import tf2_py as tf2

import common.utils as utils

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
    
    # Get all bags in a folder if the file ends with *.bag
    if len(bag_files) == 1 and bag_files[0].endswith("*.bag"):
        path = bag_files[0].replace("*.bag", "")
        bag_files = [os.path.join(path, f) for f in os.listdir(path) if os.path.isfile(os.path.join(path, f)) and f.endswith(".bag")]
        if len(bag_files) < 1:
            raise ValueError("There is no *.bag file in %s." % path)

    if output_dir is None or output_dir == "" or not os.path.exists(output_dir) or not os.path.isdir(output_dir):
        output_dir = os.path.join(os.path.abspath(os.path.dirname(bag_files[0])), "TFRecord")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        logging.info("Output directory set to %s" % output_dir)

    if image_topic is None or image_topic == "":
        logging.error("No image topic given. Use parameter image_topic.")
        return

    if tf_map is None or tf_map == "" or tf_base_link == "":
        logging.error("Please specify tf frame names.")
        return

    if images_per_bin is None or images_per_bin < 1:
        logging.error("images_per_bin has to be greater than 1.")
        return

    if -2 > label or label > 2:
        logging.error("label has to be between -2 and 2.")
        return

    for bag_file in bag_files:
        # Check parameters
        if bag_file == "" or not os.path.exists(bag_file) or not os.path.isfile(bag_file):
            logging.error("Specified bag does not exist (%s)" % bag_file)
            return

        logging.info("Extracting %s" % bag_file)

        bag_file_name = os.path.splitext(os.path.basename(bag_file))[0]

        def get_label(image, last_label, auto_duration=10):
            if label < 0: # Labeling mode (show image and wait for input)
                image_cp = image.copy()

                if label == -1 and not last_label is None:
                    utils.image_write_label(image_cp, last_label)

                cv2.imshow("Label image | [1]: No anomaly, [2]: Contains anomaly, [0]: Unknown", image_cp)
                key = cv2.waitKey(0 if label == -2 or last_label == None else auto_duration)
                
                if key == 27:   # [esc] => Quit
                    return None
                elif key == 48: # [0]   => Unknown
                    return 0
                elif key == 49: # [1]   => No anomaly
                    return 1
                elif key == 50: # [2]   => Contains anomaly
                    return 2
                elif key == -1 and label == -1 and not last_label is None:
                    return last_label
                else:
                    return get_label(image, None)
            else:
                return label

        # Used to convert image message to opencv image
        bridge = CvBridge()
        
        ################
        #     MAIN     #
        ################
        with utils.GracefulInterruptHandler() as h:
            with rosbag.Bag(bag_file, "r") as bag:
                ### Get /tf transforms
                utils.print_progress(0, 1, prefix = "Extracting transforms:")
                expected_tf_count = bag.get_message_count(["/tf", "/tf_static"])
                total_tf_count = 0
                start = time.time()
                tf_buffer = tf2_ros.Buffer(cache_time=rospy.Duration(bag.get_end_time() - bag.get_start_time()), debug=False)
                for topic, msg, t in bag.read_messages(topics=["/tf", "/tf_static"]):
                    if h.interrupted:
                        logging.warning("Interrupted!")
                        return
                    
                    for msg_tf in msg.transforms:
                        if topic == "/tf_static":
                            tf_buffer.set_transform_static(msg_tf, "default_authority")
                        else:
                            tf_buffer.set_transform(msg_tf, "default_authority")
                    total_tf_count += 1
                    
                    # Print progress
                    utils.print_progress(total_tf_count,
                                        expected_tf_count,
                                        prefix = "Extracting transforms:",
                                        suffix = "(%i / %i)" % (total_tf_count, expected_tf_count),
                                        time_start = start)

                ### Get images
                utils.print_progress(0, 1, prefix = "Writing TFRecord:")

                expected_im_count = bag.get_message_count(image_topic)
                number_of_bins = expected_im_count // images_per_bin + 1

                total_count = 0
                total_saved_count = 0
                per_bin_count = 0
                skipped_count = 0
                tfWriter = None        

                colorspace = b"RGB"
                channels = 3

                image_label = None

                start = time.time()

                for topic, msg, t in bag.read_messages(topics=image_topic):
                    if h.interrupted:
                        logging.warning("Interrupted!")
                        return
                    
                    total_count += 1
                    
                    try:
                        # Get translation and orientation
                        msg_tf = tf_buffer.lookup_transform(tf_map, tf_base_link, t)#, rospy.Duration.from_sec(0.001))
                        translation = msg_tf.transform.translation
                        euler = tf.transformations.euler_from_quaternion([msg_tf.transform.rotation.x, msg_tf.transform.rotation.y, msg_tf.transform.rotation.z, msg_tf.transform.rotation.w])

                        # Get the image
                        cv_image = bridge.imgmsg_to_cv2(msg, "bgr8")
                        _, encoded = cv2.imencode(".jpeg", cv_image)
                        
                        # Get the label     0: Unknown, 1: No anomaly, 2: Contains an anomaly
                        image_label = get_label(cv_image, image_label)
                        if image_label == None: # [esc]
                            logging.warning("Interrupted!")
                            return

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
                            tfWriter = tensorflow.io.TFRecordWriter(output_file)
                            per_bin_count = 0

                        # Add image and position to TFRecord
                        feature_dict = {
                            "metadata/location/translation/x"   : utils._float_feature(translation.x),
                            "metadata/location/translation/y"   : utils._float_feature(translation.y),
                            "metadata/location/translation/z"   : utils._float_feature(translation.z),
                            "metadata/location/rotation/x"      : utils._float_feature(euler[0]),
                            "metadata/location/rotation/y"      : utils._float_feature(euler[1]),
                            "metadata/location/rotation/z"      : utils._float_feature(euler[2]),
                            "metadata/time"                     : utils._int64_feature(t.to_nsec()), # There were some serious problems saving to_sec as float...
                            "metadata/label"                    : utils._int64_feature(image_label), # 0: Unknown, 1: No anomaly, 2: Contains an anomaly
                            "metadata/rosbag"                   : utils._bytes_feature(bag_file),
                            "metadata/tfrecord"                 : utils._bytes_feature(output_file),
                            "image/height"      : utils._int64_feature(msg.height),
                            "image/width"       : utils._int64_feature(msg.width),
                            "image/channels"    : utils._int64_feature(channels),
                            "image/colorspace"  : utils._bytes_feature(colorspace),
                            "image/format"      : utils._bytes_feature("jpeg"),
                            "image/encoded"     : utils._bytes_feature(encoded.tobytes())
                        }

                        example = tensorflow.train.Example(features=tensorflow.train.Features(feature=feature_dict))
                        
                        tfWriter.write(example.SerializeToString())
                        per_bin_count += 1
                        total_saved_count += 1

                    except tf2.ExtrapolationException:
                        skipped_count += 1

                    # Print progress
                    utils.print_progress(total_count,
                                        expected_im_count,
                                        prefix = "Writing TFRecord:",
                                        suffix = "(%i / %i, skipped %i)" % (total_saved_count, expected_im_count, skipped_count),
                                        time_start = start)

                if tfWriter:
                    tfWriter.close()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    rosbag_to_tfrecord()