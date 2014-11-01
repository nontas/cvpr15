from __future__ import division
import numpy as np
from scipy.misc import comb as nchoosek
from scipy.stats import multivariate_normal
from scipy.sparse import block_diag

from menpofit.builder import (DeformableModelBuilder, build_shape_model,
                              normalization_wrt_reference_shape)
from menpofit.base import create_pyramid
from menpofit import checks
from menpo.visualize import print_dynamic, progress_bar_str
from menpo.feature import igo
from menpo.shape import PointTree, Tree, UndirectedGraph
from menpo.image import Image

from .utils import build_patches_image, vectorize_patches_image


class APSBuilder(DeformableModelBuilder):
    def __init__(self, adjacency_array=None, root_vertex=0, features=igo,
                 patch_shape=(16, 16), normalization_diagonal=None, n_levels=3,
                 downscale=2, scaled_shape_models=True,
                 max_shape_components=None, n_appearance_parameters=None):
        # check parameters
        checks.check_n_levels(n_levels)
        checks.check_downscale(downscale)
        checks.check_normalization_diagonal(normalization_diagonal)
        max_shape_components = checks.check_max_components(
            max_shape_components, n_levels, 'max_shape_components')
        features = checks.check_features(features, n_levels)

        # flag whether to create MST
        self.tree_is_mst = adjacency_array is None
        if adjacency_array is not None:
            self.tree = Tree(adjacency_array, root_vertex)

        # store parameters
        self.root_vertex = root_vertex
        self.features = features
        self.patch_shape = patch_shape
        self.normalization_diagonal = normalization_diagonal
        self.n_levels = n_levels
        self.downscale = downscale
        self.scaled_shape_models = scaled_shape_models
        self.max_shape_components = max_shape_components
        self.n_appearance_parameters = n_appearance_parameters

    def build(self, images, group=None, label=None, verbose=False):
        # compute reference_shape and normalize images size
        self.reference_shape, normalized_images = \
            normalization_wrt_reference_shape(images, group, label,
                                              self.normalization_diagonal,
                                              verbose=verbose)

        # create pyramid
        generators = create_pyramid(normalized_images, self.n_levels,
                                    self.downscale, self.features,
                                    verbose=verbose)

        # if tree not provided, compute the MST
        shapes = [i.landmarks[group][label] for i in normalized_images]
        if self.tree_is_mst:
            self.tree = _compute_minimum_spanning_tree(shapes, self.root_vertex,
                                                       '- ', verbose)

        # build the model at each pyramid level
        if verbose:
            if self.n_levels > 1:
                print_dynamic('- Building model for each of the {} pyramid '
                              'levels\n'.format(self.n_levels))
            else:
                print_dynamic('- Building model\n')

        shape_models = []
        appearance_models = []
        deformation_models = []
        # for each pyramid level (high --> low)
        for j in range(self.n_levels):
            # since models are built from highest to lowest level, the
            # parameters in form of list need to use a reversed index
            rj = self.n_levels - j - 1

            level_str = '  - '
            if verbose:
                if self.n_levels > 1:
                    level_str = '  - Level {}: '.format(j + 1)

            # get feature images of current level
            feature_images = []
            for c, g in enumerate(generators):
                if verbose:
                    print_dynamic(
                        '{}Computing feature space/rescaling - {}'.format(
                        level_str,
                        progress_bar_str((c + 1.) / len(generators),
                                         show_bar=False)))
                feature_images.append(next(g))

            # extract potentially rescaled shapes
            shapes = [i.landmarks[group][label] for i in feature_images]

            # define shapes that will be used for training
            if j == 0:
                original_shapes = shapes
                train_shapes = shapes
            else:
                if self.scaled_shape_models:
                    train_shapes = shapes
                else:
                    train_shapes = original_shapes

            # train shape model and find reference frame
            if verbose:
                print_dynamic('{}Building shape model'.format(level_str))
            shape_model = build_shape_model(
                train_shapes, self.max_shape_components[rj])

            # add shape model to the list
            shape_models.append(shape_model)

            # compute relative locations from all shapes
            relative_locations = _get_relative_locations(
                train_shapes, self.tree, level_str, verbose)

            # build and add deformation model to the list
            deformation_models.append(_build_deformation_model(
                self.tree, relative_locations, level_str, verbose))

            # extract patches from all images
            all_patches_array = _warp_images(feature_images, group, label,
                                             self.patch_shape, level_str,
                                             verbose)

            # build and add appearance model to the list
            n_points = images[0].landmarks[group][label].n_points
            n_channels = feature_images[0].n_channels
            appearance_models.append(_build_appearance_model(
                all_patches_array, n_points, self.patch_shape, n_channels,
                self.n_appearance_parameters, level_str, verbose))

            if verbose:
                print_dynamic('{}Done\n'.format(level_str))

        # reverse the list of models so that they are ordered from lower to
        # higher resolution
        shape_models.reverse()
        appearance_models.reverse()
        deformation_models.reverse()
        n_training_images = len(images)

        return self._build_aps(shape_models, deformation_models,
                               appearance_models, n_training_images)

    def _build_aps(self, shape_models, deformation_models, appearance_models,
                   n_training_images):
        r"""
        """
        from .base import APS
        return APS(shape_models, deformation_models, appearance_models,
                   n_training_images, self.tree, self.patch_shape,
                   self.features, self.reference_shape, self.downscale,
                   self.scaled_shape_models)


def _warp_images(images, group, label, patch_shape, level_str, verbose):
    r"""
    returns numpy.array of size (68*16*16*36) x n_images
    """
    # find length of each patch and number of points
    n_points = images[0].landmarks[group][label].n_points
    patches_len = np.prod(patch_shape) * images[0].n_channels * n_points
    n_images = len(images)

    # initialize the output numpy array
    all_patches_array = np.empty((patches_len, n_images))

    # extract parts
    for c, i in enumerate(images):
        # print progress
        if verbose:
            print_dynamic('{}Extracting patches from images - {}'.format(
                level_str,
                progress_bar_str(float(c + 1) / len(images),
                                 show_bar=False)))

        # extract patches from this image
        patches_image = build_patches_image(i, None, patch_shape)

        # store
        all_patches_array[..., c] = vectorize_patches_image(patches_image)

    return all_patches_array


def _get_relative_locations(shapes, tree, level_str, verbose):
    r"""
    returns numpy.array of size 2 x n_images x n_edges
    """
    # convert given shapes to point trees
    point_trees = [PointTree(shape.points, tree.adjacency_array,
                             tree.root_vertex)
                   for shape in shapes]

    # initialize an output numpy array
    rel_loc_array = np.empty((2, tree.n_edges, len(point_trees)))

    # get relative locations
    for c, pt in enumerate(point_trees):
        # print progress
        if verbose:
            print_dynamic('{}Computing relative locations from shapes - {}'.format(
                level_str,
                progress_bar_str(float(c + 1) / len(point_trees),
                                 show_bar=False)))

        # get relative locations from this shape
        rl = pt.relative_locations()

        # store
        rel_loc_array[..., c] = rl.T

    # rollaxis and return
    return np.rollaxis(rel_loc_array, 2, 1)


def _compute_minimum_spanning_tree(shapes, root_vertex, level_str, verbose):
    # initialize edges and weights matrix
    n_vertices = shapes[0].n_points
    n_edges = nchoosek(n_vertices, 2)
    weights = np.zeros((n_vertices, n_vertices))
    edges = np.empty((n_edges, 2), dtype=np.int32)

    # fill edges and weights
    e = -1
    for i in range(n_vertices-1):
        for j in range(i+1, n_vertices, 1):
            # edge counter
            e += 1

            # print progress
            if verbose:
                print_dynamic('{}Computing complete graph`s weights - {}'.format(
                    level_str,
                    progress_bar_str(float(e + 1) / n_edges,
                                     show_bar=False)))

            # fill in edges
            edges[e, 0] = i
            edges[e, 1] = j

            # create data matrix of edge
            diffs_x = [s.points[i, 0] - s.points[j, 0] for s in shapes]
            diffs_y = [s.points[i, 1] - s.points[j, 1] for s in shapes]
            coords = np.array([diffs_x, diffs_y])

            # compute mean
            m = np.mean(coords, axis=1)

            # compute covariance
            c = np.cov(coords)

            # get weight
            for im in range(len(shapes)):
                weights[i, j] += -np.log(multivariate_normal.pdf(coords[:, im],
                                                                 mean=m, cov=c))
            weights[j, i] = weights[i, j]

    # create undirected graph
    complete_graph = UndirectedGraph(edges)

    if verbose:
        print_dynamic('{}Minimum spanning tree computed.\n'.format(level_str))

    # compute minimum spanning tree
    return complete_graph.minimum_spanning_tree(weights, root_vertex)


def _build_deformation_model(tree, relative_locations, level_str, verbose):
    # build deformation model
    if verbose:
        print_dynamic('{}Training deformation distribution per '
                      'tree edge'.format(level_str))
    def_len = 2 * tree.n_vertices
    def_cov = np.zeros((def_len, def_len))
    for e in range(tree.n_edges):
        # print progress
        if verbose:
            print_dynamic('{}Training deformation distribution '
                          'per edge - {}'.format(
                          level_str,
                          progress_bar_str(float(e + 1) / tree.n_edges,
                                           show_bar=False)))

        # get vertices adjacent to edge
        parent = tree.adjacency_array[e, 0]
        child = tree.adjacency_array[e, 1]

        # compute covariance matrix
        edge_cov = np.linalg.inv(np.cov(relative_locations[..., e]))

        # store its values
        s1 = edge_cov[0, 0]
        s2 = edge_cov[1, 1]
        s3 = 2 * edge_cov[0, 1]

        # Fill the covariance matrix matrix
        # get indices
        p1 = 2 * parent
        p2 = 2 * parent + 1
        c1 = 2 * child
        c2 = 2 * child + 1

        # up-left block
        def_cov[p1, p1] += s1
        def_cov[p2, p2] += s2
        def_cov[p2, p1] += s3

        # up-right block
        def_cov[p1, c1] = - s1
        def_cov[p2, c2] = - s2
        def_cov[p1, c2] = - s3 / 2
        def_cov[p2, c1] = - s3 / 2

        # down-left block
        def_cov[c1, p1] = - s1
        def_cov[c2, p2] = - s2
        def_cov[c1, p2] = - s3 / 2
        def_cov[c2, p1] = - s3 / 2

        # down-right block
        def_cov[c1, c1] += s1
        def_cov[c2, c2] += s2
        def_cov[c1, c2] += s3

    return def_cov


def _build_appearance_model(all_patches_array, n_points, patch_shape,
                            n_channels, n_appearance_parameters, level_str,
                            verbose):
    # build appearance model
    if verbose:
        print_dynamic('{}Training appearance distribution per '
                      'patch'.format(level_str))
    # compute mean appearance vector
    app_mean = np.mean(all_patches_array, axis=1)

    # appearance vector and patch vector lengths
    patch_len = np.prod(patch_shape) * n_channels

    # check n_appearance_parameters
    if n_appearance_parameters is None:
        n_appearance_parameters = patch_len

    # compute covariance matrix for each patch
    all_cov = []
    for e in range(n_points):
        # print progress
        if verbose:
            print_dynamic('{}Training appearance distribution '
                          'per patch - {}'.format(
                          level_str,
                          progress_bar_str(float(e + 1) / n_points,
                                           show_bar=False)))
        # find indices in target covariance matrix
        i_from = e * patch_len
        i_to = (e + 1) * patch_len

        # compute and store covariance
        cov_mat = np.cov(all_patches_array[i_from:i_to, :])
        s, v, d = np.linalg.svd(cov_mat)
        s = s[:, :n_appearance_parameters]
        v = v[:n_appearance_parameters]
        d = d[:n_appearance_parameters, :]
        all_cov.append(s.dot(np.diag(1/v)).dot(d))

    # create final sparse covariance matrix
    return app_mean, block_diag(all_cov).tocsr()
