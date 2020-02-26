# -*- coding: utf-8 -*-

import os
import logging
import time

import h5py
import numpy as np
from scipy.spatial import distance

from anomalyModelBase import AnomalyModelBase
import common.utils as utils

class AnomalyModelBalancedDistribution(AnomalyModelBase):
    """Anomaly model formed by a Balanced Distribution of feature vectors
    Reference: https://www.mdpi.com/2076-3417/9/4/757
    """
    def __init__(self, initial_normal_features=1000, threshold_learning=20, threshold_classification=5, pruning_parameter=0.5):
        AnomalyModelBase.__init__(self)
        
        assert 0 < pruning_parameter < 1, "Pruning parameter out of range (0 < η < 1)"

        self.initial_normal_features    = initial_normal_features   # See reference algorithm variable N
        self.threshold_learning         = threshold_learning        # See reference algorithm variable α
        self.threshold_classification   = threshold_classification  # See reference algorithm variable β
        self.pruning_parameter          = pruning_parameter         # See reference algorithm variable η

        self.balanced_distribution        = None          # Array containing all the "normal" samples

        self._mean = None   # Mean
        self._covI = None   # Inverse of covariance matrix

    
    def classify(self, feature, threshold_classification=None):
        """The anomaly measure is defined as the Mahalanobis distance between a feature sample
        and the Balanced Distribution.
        """
        if threshold_classification is None:
            threshold_classification = self.threshold_classification
        
        if self._mean is None or self._covI is None:
            self._calculate_mean_and_covariance()

        return self._mahalanobis_distance(feature) > threshold_classification
    
    def _calculate_mean_and_covariance(self):
        """Calculate mean and inverse of covariance of the "normal" distribution"""
        assert not self.balanced_distribution is None and len(self.balanced_distribution) > 0, \
            "Can't calculate mean or covariance of nothing!"
        
        self._mean = np.mean(self.balanced_distribution, axis=0, dtype=np.float64)    # Mean
        cov = np.cov(self.balanced_distribution, rowvar=False)                        # Covariance matrix
        self._covI = np.linalg.pinv(cov)                                            # Inverse of covariance matrix
    
    def _mahalanobis_distance(self, feature):
        """Calculate the Mahalanobis distance between the input and the model"""
        assert not self._covI is None and not self._mean is None, \
            "You need to load a model before computing a Mahalanobis distance"

        assert feature.shape[0] == self._mean.shape[0] == self._covI.shape[0] == self._covI.shape[1], \
            "Shapes don't match (x: %s, μ: %s, Σ¯¹: %s)" % (feature.shape, self._mean.shape, self._covI.shape)
        
        return distance.mahalanobis(feature, self._mean, self._covI)


    def generate_model(self, features):
        AnomalyModelBase.generate_model(self, features) # Call base

        # Reduce features to simple list
        features_flat = features.flatten()

        logging.info("Generating a Balanced Distribution from %i feature vectors of length %i" % (features_flat.shape[0], len(features_flat[0])))

        assert features_flat.shape[0] > self.initial_normal_features, \
            "Not enough initial features provided. Please decrease initial_normal_features (%i)" % self.initial_normal_features

        with utils.GracefulInterruptHandler() as h:
            # Create initial set of "normal" vectors
            self.balanced_distribution = features_flat[:self.initial_normal_features]

            start = time.time()

            self._calculate_mean_and_covariance()
            
            # loggin.info(np.mean(np.array([self._mahalanobis_distance(f) for f in features_flat])))

            utils.print_progress(0, 1, prefix = "%i / %i" % (self.initial_normal_features, features_flat.shape[0]))
            
            # Loop over the remaining feature vectors
            for index, feature in enumerate(features_flat[self.initial_normal_features:]):
                if h.interrupted:
                    logging.warning("Interrupted!")
                    self.balanced_distribution = None
                    return False

                # Calculate the Mahalanobis distance to the "normal" distribution
                dist = self._mahalanobis_distance(feature)
                if dist > self.threshold_learning:
                    # Add the vector to the "normal" distribution
                    self.balanced_distribution = np.append(self.balanced_distribution, [feature], axis=0)

                    # Recalculate mean and covariance
                    self._calculate_mean_and_covariance()
                
                # Print progress
                utils.print_progress(index + self.initial_normal_features + 1,
                                     features_flat.shape[0],
                                     prefix = "%i / %i" % (index + self.initial_normal_features + 1, features_flat.shape[0]),
                                     suffix = "%i vectors in Balanced Distribution" % len(self.balanced_distribution),
                                     time_start = start)

            # Prune the distribution
            
            logging.info(np.mean(np.array([self._mahalanobis_distance(f) for f in self.balanced_distribution])))

            prune_filter = []
            pruned = 0
            logging.info("Pruning Balanced Distribution")
            utils.print_progress(0, 1, prefix = "%i / %i" % (self.initial_normal_features, features_flat.shape[0]))
            start = time.time()

            for index, feature in enumerate(self.balanced_distribution):
                if h.interrupted:
                    logging.warning("Interrupted!")
                    self.balanced_distribution = None
                    return False

                prune = self._mahalanobis_distance(feature) < self.threshold_learning * self.pruning_parameter
                prune_filter.append(prune)

                if prune:
                    pruned += 1

                # Print progress
                utils.print_progress(index + 1,
                                     len(self.balanced_distribution),
                                     prefix = "%i / %i" % (index + 1, len(self.balanced_distribution)),
                                     suffix = "%i vectors pruned" % pruned,
                                     time_start = start)

            self.balanced_distribution = self.balanced_distribution[prune_filter]

            logging.info("Generated Balanced Distribution with %i entries" % len(self.balanced_distribution))
        
            self._calculate_mean_and_covariance()
            return True

        
    def __load_model_from_file__(self, h5file):
        """Load a Balanced Distribution from file"""
        self.balanced_distribution        = np.array(h5file["balanced_distribution"])
        self.initial_normal_features    = h5file.attrs["initial_normal_features"]
        self.threshold_learning         = h5file.attrs["threshold_learning"]
        self.threshold_classification   = h5file.attrs["threshold_classification"]
        self.pruning_parameter          = h5file.attrs["pruning_parameter"]
        assert 0 < self.pruning_parameter < 1, "Pruning parameter out of range (0 < η < 1)"
        self._calculate_mean_and_covariance()
        logging.info("Successfully loaded Balanced Distribution with %i entries and %i dimensions" % (len(self.balanced_distribution), self.balanced_distribution[0].shape[0]))
    
    def save_model_to_file(self, h5file):
        """Save the model to disk"""
        h5ffile.create_dataset("balanced_distribution",        data=self.balanced_distribution, dtype=np.float64)
        h5ffile.attrs["initial_normal_features"]    = self.initial_normal_features
        h5ffile.attrs["threshold_learning"]         = self.threshold_learning
        h5ffile.attrs["threshold_classification"]   = self.threshold_classification
        h5ffile.attrs["pruning_parameter"]          = self.pruning_parameter

# Only for tests
if __name__ == "__main__":
    from anomalyModelTest import AnomalyModelTest
    model = AnomalyModelBalancedDistribution()
    test = AnomalyModelTest(model)

    # test.calculateMahalobisDistances()
    def _feature_to_color(feature):
        b = 100 if feature in model.balanced_distribution else 0
        g = 0
        r = model._mahalanobis_distance(feature) * (255 / 60)
        #r = 100 if self.model._mahalanobis_distance(feature) > threshold else 0
        return (b, g, r)

    def _pause(feature):
        return feature in test.model.balanced_distribution

    test.visualize(threshold=60, feature_to_color_func=_feature_to_color)