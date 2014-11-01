import cPickle

from menpo.image import Image


def build_patches_image(image, centres, patch_shape):
    r"""
    Return the image patches as a menpo.image object
    size: n_points x patch_shape[0] x patch_shape[1] x n_channels
    """
    # extract patches
    if centres is None:
        patches = image.extract_patches_around_landmarks(patch_size=patch_shape,
                                                         as_single_array=True)
    else:
        patches = image.extract_patches(centres, patch_size=patch_shape,
                                        as_single_array=True)

    # build patches image
    return Image(patches)


def vectorize_patches_image(patches_image):
    r"""
    Return the image patches vector by calling the menpo.image.Image.as_vector()
    size: (n_points * patch_shape[0] * patch_shape[1] * n_channels) x 1
    """
    return patches_image.as_vector()


def pickle_load(path):
    with open(str(path), 'rb') as f:
        return cPickle.load(f)


def pickle_dump(obj, path):
    with open(str(path), 'wb') as f:
        cPickle.dump(obj, f, protocol=2)
