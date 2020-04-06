import os
import time
import sys

import h5py
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

import consts
from common import PatchArray, Visualize, utils, logger

class AnomalyModelBase(object):
    
    def __init__(self):
        self.NAME = self.__class__.__name__.replace("AnomalyModel", "")
        self.patches = None
    
    def __generate_model__(self, patches, silent=False):
        """Generate a model based on the features and metadata
        
        Args:
            patches (PatchArray): Array of patches with features as extracted by a FeatureExtractor

        Returns:
            h5py.group that will be saved to the features file
        """
        raise NotImplementedError()
        
    def __mahalanobis_distance__(self, patch):
        """Calculate the Mahalanobis distance between the input and the model"""
        raise NotImplementedError()
        
    def classify(self, patch):
        """Classify a single feature based on the loaded model
        
        Args:
            patch (np.record): A single patch (with feature)

        Returns:
            A label
        """
        raise NotImplementedError()
    
    def __save_model_to_file__(self, h5file):
        """ Internal method that should be implemented by subclasses and save the
        necessary information to the file so that the model can be reloaded later
        """
        raise NotImplementedError()

    def __load_model_from_file__(self, h5file):
        """ Internal method that should be implemented by subclasses and load the
        necessary information from the file
        """
        raise NotImplementedError()
    
    ########################
    # Common functionality #
    ########################
    
    def load_or_generate(self, patches=consts.FEATURES_FILE,
                               load_patches=False, silent=False):
        """Load a model from file or generate it based on the features
        
        Args:
            patches (str, PatchArray) : HDF5 file containing features (see feature_extractor for details)
        """
        
        # Load patches if necessary
        if isinstance(patches, basestring):
            if patches == "" or not os.path.exists(patches) or not os.path.isfile(patches):
                raise ValueError("Specified file does not exist (%s)" % patches)
            
            # Try loading
            loaded = self.load_from_file(patches, load_patches=load_patches)
            if loaded:
                return True

            # Read file
            if not isinstance(self.patches, PatchArray):
                patches = PatchArray(patches)
        elif isinstance(patches, PatchArray):
            self.patches = patches
            # Try loading
            loaded = self.load_from_file(patches.filename, load_patches=load_patches)
            if loaded:
                return True
        else:
            raise ValueError("patches must be a path to a file or a PatchArray.")
        
        assert patches.contains_features, "patches must contain features to calculate an anomaly model."

        self.patches = patches

        p = self.patches[:, 0, 0]

        f = np.zeros(p.shape, dtype=np.bool)
        f[:] = np.logical_and(p.labels == 1,                        # No anomaly and
                              np.logical_or(p.round_numbers == 7,   #     Round 7
                                            p.round_numbers == 9))  #        or 9

        model_input = patches[f]

        start = time.time()

        # Generate model
        if self.__generate_model__(model_input, silent=silent) == False:
            logger.info("Could not generate model.")
            return False

        end = time.time()

        logger.info("Writing model to: %s" % self.patches.filename)
        with h5py.File(self.patches.filename, "a") as hf:
            g = hf.get(self.NAME)

            if g is not None:
                del hf[self.NAME]
            
            g = hf.create_group(self.NAME)

            # Add metadata to the output file
            g.attrs["Number of features used"]   = model_input.size

            computer_info = utils.getComputerInfo()
            for key, value in computer_info.items():
                g.attrs[key] = value

            g.attrs["Start"] = start
            g.attrs["End"] = end
            g.attrs["Duration"] = end - start
            g.attrs["Duration (formatted)"] = utils.format_duration(end - start)

            self.__save_model_to_file__(g)
        logger.info("Successfully written model to: %s" % self.patches.filename)

        self.calculate_mahalanobis_distances()

        return True

    def is_in_file(self, model_file):
        """ Check if model and mahalanobis distances are already in model_file """
        with h5py.File(model_file, "r") as hf:
            g = hf.get(self.NAME)
            
            if g is None:
                return (False, False)
            else:
                return (True, "mahalanobis_distances" in g.keys())

    def load_from_file(self, model_file, load_patches=False):
        """ Load a model from file """
        with h5py.File(model_file, "r") as hf:
            g = hf.get(self.NAME)
            
            if g is None:
                return False
            
            logger.info("Reading model from: %s" % model_file)

            if load_patches:
                self.patches = PatchArray(model_file)

            return self.__load_model_from_file__(g)
    
    def calculate_mahalanobis_distances(self):
        """ Calculate all the Mahalanobis distances and save them to the file """
        with h5py.File(self.patches.filename, "r+") as hf:
            g = hf.get(self.NAME)

            if g is None:
                raise ValueError("The model needs to be saved first")
            
            maha = np.zeros(self.patches.shape, dtype=np.float64)
            
            for i in tqdm(np.ndindex(self.patches.shape), desc="Calculating mahalanobis distances", total=self.patches.size, file=sys.stderr):
                maha[i] = self.__mahalanobis_distance__(self.patches[i])

            no_anomaly = maha[self.patches.labels == 1]
            anomaly = maha[self.patches.labels == 2]

            if g.get("mahalanobis_distances") is not None: del g["mahalanobis_distances"]
            m = g.create_dataset("mahalanobis_distances", data=maha)
            m.attrs["max_no_anomaly"] = np.nanmax(no_anomaly)
            m.attrs["max_anomaly"]    = np.nanmax(anomaly)

            # hist1, bins = np.histogram(no_anomaly, bins="fd")
            # m.attrs["histogram_no_anomaly"] = hist1

            # hist2, _ = np.histogram(anomaly, bins=bins)
            # m.attrs["histogram_anomaly"] = hist2
            
            # m.attrs["histogram_edges"] = bins

            logger.info("Saved Mahalanobis distances to file")
            return True

    def visualize(self, **kwargs):
        """ Visualize the result of a anomaly model """

        if "threshold" not in kwargs:
            kwargs["threshold"] = 60
        
        if "patch_to_color_func" not in kwargs:
            def _default_patch_to_color(v, patch):
                b = 0#100 if patch in self.normal_distribution else 0
                g = 0
                threshold = v.get_trackbar("threshold")
                if v.get_trackbar("show_thresh"):
                    r = 100 if self.__mahalanobis_distance__(patch) > threshold else 0
                elif threshold == 0:
                    r = 0
                else:
                    r = min(255, int(self.__mahalanobis_distance__(patch) * (255 / threshold)))
                return (b, g, r)
            kwargs["patch_to_color_func"] = _default_patch_to_color

        if "patch_to_text_func" not in kwargs:
            def _default_patch_to_text(v, patch):
                return round(self.__mahalanobis_distance__(patch), 2)
            kwargs["patch_to_text_func"] = _default_patch_to_text

        vis = Visualize(self.patches, **kwargs)

        vis.create_trackbar("threshold", int(kwargs["threshold"]), 1000)
        vis.create_trackbar("show_thresh", 1, 1)
        
        vis.show()