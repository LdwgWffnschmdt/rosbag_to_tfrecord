import tensorflow as tf
import tensorflow_hub as hub

from featureExtractorBase import FeatureExtractorBase

from Models.C3D.c3d import C3D
from Models.C3D.sports1M_utils import preprocess_input

class FeatureExtractorC3D(FeatureExtractorBase):
    """Feature extractor based on C3D (trained on sports1M).
    Output layer: conv5b + MaxPooling3D to reduce frames
    Generates 7x7x512 feature vectors per temporal image batch
    """
    __layer__ = "conv5b"

    def __init__(self):
        FeatureExtractorBase.__init__(self)

        self.IMG_SIZE = 112           # All images will be resized to 112x112
        self.BATCH_SIZE = 64
        self.TEMPORAL_BATCH_SIZE = 16 # Fixed for C3D

        # Create the base model from the pre-trained C3D
        model_full = C3D(weights='sports1M')
        model_full.trainable = False

        output = model_full.get_layer(self.__layer__).output
        pool_size = (output.shape[1], 1, 1)
        output = tf.keras.layers.MaxPooling3D(pool_size=pool_size, strides=pool_size, padding='valid', name='reduce_frames')(output)

        self.model = tf.keras.Model(model_full.inputs, output)
        self.model.trainable = False
    
    def __transform_dataset__(self, dataset):
        temporal_image_windows = dataset.map(lambda image, *args: image).window(self.TEMPORAL_BATCH_SIZE, 1, 1, True)
        temporal_image_windows = temporal_image_windows.flat_map(lambda window: window.batch(self.TEMPORAL_BATCH_SIZE))

        matching_meta_stuff    = dataset.map(lambda image, *args: args).skip(self.TEMPORAL_BATCH_SIZE - 1)
        return tf.data.Dataset.zip((temporal_image_windows, matching_meta_stuff)).map(lambda image, meta: (image,) + meta)

    def extract_batch(self, batch):
        return tf.squeeze(self.model(batch))

# Only for tests
if __name__ == "__main__":
    extractor = FeatureExtractorC3D()
    extractor.plot_model(extractor.model)
    extractor.extract_files()