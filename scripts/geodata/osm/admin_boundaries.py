'''
admin_boundaries.py
-------------------

Generates polygons from OpenStreetMap relations
'''

import array
import logging
import six

from bisect import bisect_left
from collections import defaultdict, OrderedDict
from itertools import izip, combinations

from geodata.coordinates.conversion import latlon_to_decimal
from geodata.file_utils import ensure_dir
from geodata.graph.scc import strongly_connected_components
from geodata.math.floats import isclose
from geodata.osm.extract import *


class OSMPolygonReader(object):
    '''
    OSM relations are stored with pointers to their bounding ways,
    which in turn store pointers to their constituent nodes and the
    XML file for planet is far too large to be parsed in-memory.

    For the purposes of constructing (multi)polygons, we need lists
    of lat/lon coordinates for the edges of each outer and inner polygon
    that form the overall boundary (this allows for holes e.g.
    Lesotho/South Africa and multiple disjoint polygons such as islands)

    This class creates a compact representation of the intermediate
    lookup tables and coordinates using Python's typed array module
    which stores C-sized ints, doubles, etc. in a dynamic array. It's like
    a list but smaller and faster for arrays of numbers and doesn't require
    pulling in numpy as a dependency when all we want is the space savings.

    One nice property of the .osm files generated by osmfilter is that
    nodes/ways/relations are stored in sorted order, so we don't have to
    pre-sort the lookup arrays before performing binary search.
    '''

    def __init__(self, filename):
        self.filename = filename

        self.node_ids = array.array('l')
        self.way_ids = array.array('l')

        self.coords = array.array('d')

        self.nodes = {}

        self.way_deps = array.array('l')
        self.way_coords = array.array('d')
        self.way_indptr = array.array('i', [0])

        self.logger = logging.getLogger('osm_admin_polys')

    def binary_search(self, a, x):
        '''Locate the leftmost value exactly equal to x'''
        i = bisect_left(a, x)
        if i != len(a) and a[i] == x:
            return i
        raise ValueError

    def node_coordinates(self, coords, indptr, idx):
        start_index = indptr[idx] * 2
        end_index = indptr[idx + 1] * 2
        node_coords = coords[start_index:end_index]
        return zip(node_coords[::2], node_coords[1::2])

    def sparse_deps(self, data, indptr, idx):
        return [data[i] for i in xrange(indptr[idx], indptr[idx + 1])]

    def create_polygons(self, ways):
        '''
        Polygons (relations) are effectively stored as lists of
        line segments (ways) and there may be more than one polygon
        (island chains, overseas territories).

        If we view the line segments as a graph (any two ways which
        share a terminal node are connected), then the process of
        constructing polygons reduces to finding strongly connected
        components in a graph.

        https://en.wikipedia.org/wiki/Strongly_connected_component

        Note that even though there may be hundreds of thousands of
        points in a complex polygon like a country boundary, we only
        need to build a graph of connected ways, which will be many
        times smaller and take much less time to traverse.
        '''
        end_nodes = defaultdict(list)
        polys = []

        way_indices = {}
        start_end_nodes = {}

        for way_id in ways:
            # Find the way position via binary search
            try:
                way_index = self.binary_search(self.way_ids, way_id)
            except ValueError:
                continue

            # Cache the way index
            way_indices[way_id] = way_index

            # way_indptr is a compressed index into way_deps/way_coords
            # way_index i is stored at indices way_indptr[i]:way_indptr[i+1]
            # in way_deps
            start_node_id = self.way_deps[self.way_indptr[way_index]]
            end_node_id = self.way_deps[self.way_indptr[way_index + 1] - 1]

            start_end_nodes[way_id] = (start_node_id, end_node_id)

            if start_node_id == end_node_id:
                way_node_points = self.node_coordinates(self.way_coords, self.way_indptr, way_index)
                polys.append(way_node_points)
                continue

            end_nodes[start_node_id].append(way_id)
            end_nodes[end_node_id].append(way_id)

        # Way graph for a single polygon, don't need to be as concerned about storage
        way_graph = defaultdict(OrderedDict)

        for node_id, ways in end_nodes.iteritems():
            for w1, w2 in combinations(ways, 2):
                way_graph[w1][w2] = None
                way_graph[w2][w1] = None

        way_graph = {v: w.keys() for v, w in way_graph.iteritems()}

        for component in strongly_connected_components(way_graph):
            poly_nodes = []

            seen = set()

            if not component:
                continue

            q = [(c, False) for c in component[:1]]
            while q:
                way_id, reverse = q.pop()
                way_index = way_indices[way_id]

                node_coords = self.node_coordinates(self.way_coords, self.way_indptr, way_index)

                head, tail = start_end_nodes[way_id]

                if reverse:
                    node_coords = node_coords[::-1]
                    head, tail = tail, head

                for neighbor in way_graph[way_id]:
                    if neighbor in seen:
                        continue
                    neighbor_head, neighbor_tail = start_end_nodes[neighbor]
                    neighbor_reverse = neighbor_head == head or neighbor_tail == tail
                    q.append((neighbor, neighbor_reverse))

                way_start = 0 if q else 1
                poly_nodes.extend(node_coords[way_start:-1])

                seen.add(way_id)

            polys.append(poly_nodes)

        return polys

    def include_polygon(self, props):
        raise NotImplementedError('Children must implement')

    def polygons(self, properties_only=False):
        '''
        Generator which yields tuples like:

        (relation_id, properties, outer_polygons, inner_polygons)

        At this point a polygon is a list of coordinate tuples,
        suitable for passing to shapely's Polygon constructor
        but may be used for other purposes.

        outer_polygons is a list of the exterior polygons for this
        boundary. inner_polygons is a list of "holes" in the exterior
        polygons although donuts and donut-holes need to be matched
        by the caller using something like shapely's contains.
        '''
        i = 0

        for element_id, props, deps in parse_osm(self.filename, dependencies=True):
            props = {safe_decode(k): safe_decode(v) for k, v in six.iteritems(props)}
            if element_id.startswith('node'):
                node_id = long(element_id.split(':')[-1])
                lat = props.get('lat')
                lon = props.get('lon')
                if lat is None or lon is None:
                    continue
                lat, lon = latlon_to_decimal(lat, lon)
                if lat is None or lon is None:
                    continue

                if isclose(lon, 180.0):
                    lon = 179.999

                if 'name' in props and 'place' in props:
                    self.nodes[node_id] = props

                # Nodes are stored in a sorted array, coordinate indices are simply
                # [lon, lat, lon, lat ...] so the index can be calculated as 2 * i
                # Note that the pairs are lon, lat instead of lat, lon for geometry purposes
                self.coords.append(lon)
                self.coords.append(lat)
                self.node_ids.append(node_id)
            elif element_id.startswith('way'):
                way_id = long(element_id.split(':')[-1])

                # Get node indices by binary search
                try:
                    node_indices = [self.binary_search(self.node_ids, node_id) for node_id in deps]
                except ValueError:
                    continue

                # Way ids stored in a sorted array
                self.way_ids.append(way_id)

                # way_deps is the list of dependent node ids
                # way_coords is a copy of coords indexed by way ids
                for node_id, node_index in izip(deps, node_indices):
                    self.way_deps.append(node_id)
                    self.way_coords.append(self.coords[node_index * 2])
                    self.way_coords.append(self.coords[node_index * 2 + 1])

                self.way_indptr.append(len(self.way_deps))

                if deps[0] == deps[-1] and self.include_polygon(props):
                    way_id_offset = WAY_OFFSET + way_id
                    if not properties_only:
                        outer_polys = self.create_polygons([way_id])
                        inner_polys = []
                        yield way_id_offset, props, {}, outer_polys, inner_polys
                    else:
                        yield way_id_offset, props, {}

            elif element_id.startswith('relation'):
                if self.node_ids is not None:
                    self.node_ids = None
                if self.coords is not None:
                    self.coords = None

                relation_id = long(element_id.split(':')[-1])
                if len(deps) == 0 or not self.include_polygon(props) or props.get('type', '').lower() == 'multilinestring':
                    continue

                outer_ways = []
                inner_ways = []

                for elem_id, role in deps:
                    if role in ('outer', ''):
                        outer_ways.append(elem_id)
                    elif role == 'inner':
                        inner_ways.append(elem_id)
                    elif role == 'admin_centre':
                        val = self.nodes.get(long(elem_id))
                        if val is not None:
                            val['id'] = long(elem_id)
                            admin_centers.append(val)

                admin_center = {}
                if len(admin_centers) == 1:
                    admin_center = admin_centers[0]

                relation_id_offset = RELATION_OFFSET + relation_id
                if not properties_only:
                    outer_polys = self.create_polygons(outer_ways)
                    inner_polys = self.create_polygons(inner_ways)
                    yield relation_id_offset, props, admin_center, outer_polys, inner_polys
                else:
                    yield relation_id_offset, props, admin_center
            if i % 1000 == 0 and i > 0:
                self.logger.info('doing {}s, at {}'.format(element_id.split(':')[0], i))
            i += 1


class OSMAdminPolygonReader(OSMPolygonReader):
    def include_polygon(self, props):
        return 'boundary' in props or 'place' in props


class OSMSubdivisionPolygonReader(OSMPolygonReader):
    def include_polygon(self, props):
        return 'landuse' in props or 'place' in props or 'amenity' in props


class OSMBuildingPolygonReader(OSMPolygonReader):
    def include_polygon(self, props):
        return 'building' in props or 'building:part' in props or props.get('type', None) == 'building'
