#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.

import struct
import math
from typing import List, Optional

from shapely.coords import CoordinateSequence
from shapely.geometry import Point
from shapely.geometry import LineString
from shapely.geometry import Polygon
from shapely.geometry import LinearRing
from shapely.geometry import MultiPoint
from shapely.geometry import MultiLineString
from shapely.geometry import MultiPolygon
from shapely.geometry import GeometryCollection
from shapely.wkt import loads as wkt_loads


class GeometryTypeID:
    """
    Constants used to identify the geometry type in the serialized bytearray of geometry.
    """
    POINT = 1
    LINESTRING = 2
    POLYGON = 3
    MULTIPOINT = 4
    MULTILINESTRING = 5
    MULTIPOLYGON = 6
    GEOMETRYCOLLECTION = 7


class CoordinateType:
    """
    Constants used to identify geometry dimensions in the serialized bytearray of geometry.
    """
    XY = 1
    XYZ = 2
    XYM = 3
    XYZM = 4

    BYTES_PER_COORDINATE = [16, 24, 24, 32]

    @staticmethod
    def type_of(coords: CoordinateSequence, has_z: bool) -> int:
        if not coords:
            return CoordinateType.XY
        coord = coords[0]
        len_coord = len(coord)
        if len_coord == 2:
            return CoordinateType.XY
        elif len_coord == 3:
            if has_z:
                return CoordinateType.XYZ
            else:
                # This branch will never be reached since the current version of shapely
                # does not support M dimension
                return CoordinateType.XYM
        elif len_coord == 4:
            # This branch will never be reached since the current version of shapely
            # does not support M dimension
            return CoordinateType.XYZM
        else:
            raise ValueError("Invalid coordinate dimension: {}".format(coord))

    @staticmethod
    def bytes_per_coord(coord_type: int) -> int:
        return CoordinateType.BYTES_PER_COORDINATE[coord_type - 1]


class GeometryBuffer:
    buffer: bytearray
    coord_type: int
    bytes_per_coord: int
    num_coords: int
    coords_offset: int
    ints_offset: int

    def __init__(self, buffer: bytearray, coord_type: int, coords_offset: int, num_coords: int):
        self.buffer = buffer
        self.coord_type = coord_type
        self.bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
        self.num_coords = num_coords
        self.coords_offset = coords_offset
        self.ints_offset = coords_offset + self.bytes_per_coord * num_coords

    def read_linestring(self) -> LineString:
        num_points = self.read_int()
        return LineString(self.read_coordinates(num_points))

    def read_linearring(self) -> LineString:
        num_points = self.read_int()
        return LinearRing(self.read_coordinates(num_points))

    def read_polygon(self) -> Polygon:
        num_rings = self.read_int()
        if num_rings == 0:
            return Polygon()
        exterior = self.read_linearring()
        interiors = []
        for i in range(num_rings - 1):
            interiors.append(self.read_linearring())
        return Polygon(exterior, holes=interiors)

    def write_linestring(self, line):
        coords = line.coords
        self.write_int(len(coords))
        self.coords_offset = put_coordinates(self.buffer, self.coords_offset, self.coord_type, coords)

    def write_polygon(self, polygon: Polygon):
        exterior = polygon.exterior
        if not exterior.coords:
            self.write_int(0)
            return
        self.write_int(len(polygon.interiors) + 1)
        self.write_linestring(exterior)
        for interior in polygon.interiors:
            self.write_linestring(interior)

    def read_coordinates(self, num_coords: int) -> List[tuple]:
        coords = get_coordinates(self.buffer, self.coords_offset, self.coord_type, num_coords)
        self.coords_offset += num_coords * self.bytes_per_coord
        return coords

    def read_coordinate(self) -> tuple:
        coord = get_coordinate(self.buffer, self.coords_offset, self.coord_type)
        self.coords_offset += self.bytes_per_coord
        return coord

    def read_int(self) -> int:
        value = struct.unpack_from("i", self.buffer, self.ints_offset)[0]
        self.ints_offset += 4
        return value

    def write_int(self, value: int):
        struct.pack_into("i", self.buffer, self.ints_offset, value)
        self.ints_offset += 4


def serialize(geom) -> Optional[bytearray]:
    """
    Serialize a shapely geometry object to the internal representation of GeometryUDT.
    :param geom: shapely geometry object
    :return: internal representation of GeometryUDT
    """
    if geom is None:
        return None
    geom_type = geom.geom_type
    if geom_type == 'Point':
        return serialize_point(geom)
    elif geom_type == 'LineString':
        return serialize_linestring(geom)
    elif geom_type == 'Polygon':
        return serialize_polygon(geom)
    elif geom_type == 'MultiPoint':
        return serialize_multi_point(geom)
    elif geom_type == 'MultiLineString':
        return serialize_multi_linestring(geom)
    elif geom_type == 'MultiPolygon':
        return serialize_multi_polygon(geom)
    elif geom_type == 'GeometryCollection':
        return serialize_geometry_collection(geom)
    else:
        raise ValueError("Unsupported geometry type: " + geom_type)


def deserialize(buffer: bytearray):
    """
    Deserialize a shapely geometry object from the internal representation of GeometryUDT.
    :param buffer: internal representation of GeometryUDT
    :return: shapely geometry object
    """
    if buffer is None:
        return None
    preamble_byte = buffer[0]
    geom_type = (preamble_byte >> 4) & 0x0F
    coord_type = (preamble_byte >> 1) & 0x07
    num_coords = struct.unpack_from('i', buffer, 4)[0]
    geom_buffer = GeometryBuffer(buffer, coord_type, 8, num_coords)
    if geom_type == GeometryTypeID.POINT:
        geom = deserialize_point(geom_buffer)
    elif geom_type == GeometryTypeID.LINESTRING:
        geom = deserialize_linestring(geom_buffer)
    elif geom_type == GeometryTypeID.POLYGON:
        geom = deserialize_polygon(geom_buffer)
    elif geom_type == GeometryTypeID.MULTIPOINT:
        geom = deserialize_multi_point(geom_buffer)
    elif geom_type == GeometryTypeID.MULTILINESTRING:
        geom = deserialize_multi_linestring(geom_buffer)
    elif geom_type == GeometryTypeID.MULTIPOLYGON:
        geom = deserialize_multi_polygon(geom_buffer)
    elif geom_type == GeometryTypeID.GEOMETRYCOLLECTION:
        geom = deserialize_geometry_collection(geom_buffer)
    else:
        raise ValueError("Unsupported geometry type ID: {}".format(geom_type))
    return geom, geom_buffer.ints_offset


def create_buffer_for_geom(geom_type: int, coord_type: int, size: int, num_coords: int) -> bytearray:
    buffer = bytearray(size)
    preamble_byte = (geom_type << 4) | (coord_type << 1)
    buffer[0] = preamble_byte
    struct.pack_into('i', buffer, 4, num_coords)
    return buffer


def put_coordinates(buffer: bytearray, offset: int, coord_type: int, coords: CoordinateSequence):
    for coord in coords:
        offset = put_coordinate(buffer, offset, coord_type, coord)
    return offset


def put_coordinate(buffer: bytearray, offset: int, coord_type: int, coord: tuple):
    x = coord[0]
    y = coord[1]
    z = coord[2] if len(coord) > 2 else math.nan
    if coord_type == CoordinateType.XY:
        struct.pack_into('d', buffer, offset, x)
        struct.pack_into('d', buffer, offset + 8, y)
        offset += 16
    elif coord_type == CoordinateType.XYZ:
        struct.pack_into('d', buffer, offset, x)
        struct.pack_into('d', buffer, offset + 8, y)
        struct.pack_into('d', buffer, offset + 16, z)
        offset += 24
    else:
        # Shapely does not support M dimension for now
        raise ValueError("Invalid coordinate type: {}".format(coord_type))
    return offset


def get_coordinates(buffer: bytearray, offset: int, coord_type: int, num_coords: int) -> List[tuple]:
    coords = []
    bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
    for i in range(num_coords):
        coord = get_coordinate(buffer, offset, coord_type)
        coords.append(coord)
        offset += bytes_per_coord
    return coords


def get_coordinate(buffer: bytearray, offset: int, coord_type: int) -> tuple:
    x = struct.unpack_from('d', buffer, offset)[0]
    y = struct.unpack_from('d', buffer, offset + 8)[0]
    # Shapely does not support M dimension for now, so we'll simply ignore them
    if coord_type == CoordinateType.XY or coord_type == CoordinateType.XYM:
        return x, y
    elif coord_type == CoordinateType.XYZ or coord_type == CoordinateType.XYZM:
        z = struct.unpack_from('d', buffer, offset + 16)[0]
        return x, y, z
    else:
        raise NotImplementedError("XYM or XYZM coordinates were not supported")


def aligned_offset(offset):
    return (offset + 7) & ~7


def serialize_point(geom: Point) -> bytearray:
    coords = geom.coords
    if not coords:
        return create_buffer_for_geom(GeometryTypeID.POINT, CoordinateType.XY, 8, 0)
    coord_type = CoordinateType.type_of(coords, geom.has_z)
    bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
    size = 8 + bytes_per_coord
    buffer = create_buffer_for_geom(GeometryTypeID.POINT, coord_type, size, 1)
    put_coordinates(buffer, 8, coord_type, coords)
    return buffer


def deserialize_point(geom_buffer: GeometryBuffer) -> Point:
    if geom_buffer.num_coords == 0:
        # Here we don't call Point() directly since it would create an empty GeometryCollection
        # in shapely 1.x. You'll find similar code for creating empty geometries in other
        # deserialization functions.
        return wkt_loads("POINT EMPTY")
    coord = geom_buffer.read_coordinate()
    return Point(coord)


def serialize_multi_point(geom: MultiPoint) -> bytearray:
    points = geom.geoms
    num_points = len(points)
    if num_points == 0:
        return create_buffer_for_geom(GeometryTypeID.MULTIPOINT, CoordinateType.XY, 8, 0)
    coord_type = CoordinateType.type_of(points[0].coords, geom.has_z)
    bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
    size = 8 + bytes_per_coord * num_points
    buffer = create_buffer_for_geom(GeometryTypeID.MULTIPOINT, coord_type, size, num_points)
    offset = 8
    for point in points:
        coords = point.coords
        coord = coords[0] if coords else [(math.nan, math.nan)]
        offset = put_coordinate(buffer, offset, coord_type, coord)
    return buffer


def deserialize_multi_point(geom_buffer: GeometryBuffer) -> MultiPoint:
    if geom_buffer.num_coords == 0:
        return wkt_loads("MULTIPOINT EMPTY")
    coords = geom_buffer.read_coordinates(geom_buffer.num_coords)
    points = []
    for coord in coords:
        if math.isnan(coord[0]):
            # Shapely does not allow creating MultiPoint with empty components
            pass
        else:
            points.append(Point(coord))
    return MultiPoint(points)


def serialize_linestring(geom: LineString) -> bytearray:
    coords = geom.coords
    coord_type = CoordinateType.type_of(coords, geom.has_z)
    bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
    size = 8 + len(coords) * bytes_per_coord
    buffer = create_buffer_for_geom(GeometryTypeID.LINESTRING, coord_type, size, len(coords))
    put_coordinates(buffer, 8, coord_type, coords)
    return buffer


def deserialize_linestring(geom_buffer: GeometryBuffer) -> LineString:
    if geom_buffer.num_coords == 0:
        return wkt_loads("LINESTRING EMPTY")
    coords = geom_buffer.read_coordinates(geom_buffer.num_coords)
    return LineString(coords)


def serialize_multi_linestring(geom: MultiLineString) -> bytearray:
    linestrings = geom.geoms
    if not linestrings:
        return create_buffer_for_geom(GeometryTypeID.MULTILINESTRING, CoordinateType.XY, 8, 0)
    coord_type = CoordinateType.type_of(linestrings[0].coords, geom.has_z)
    bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
    num_coords = sum(len(ls.coords) for ls in linestrings)
    num_linestrings_offset = 8 + num_coords * bytes_per_coord
    size = num_linestrings_offset + 4 + 4 * len(linestrings)
    buffer = create_buffer_for_geom(GeometryTypeID.MULTILINESTRING, coord_type, size, num_coords)
    geom_buffer = GeometryBuffer(buffer, coord_type, 8, num_coords)
    geom_buffer.write_int(len(linestrings))
    for ls in linestrings:
        geom_buffer.write_linestring(ls)
    return buffer


def deserialize_multi_linestring(geom_buffer: GeometryBuffer) -> MultiLineString:
    if geom_buffer.num_coords == 0:
        return wkt_loads("MULTILINESTRING EMPTY")
    num_linestrings = geom_buffer.read_int()
    linestrings = []
    for k in range(0, num_linestrings):
        linestring = geom_buffer.read_linestring()
        if not linestring.is_empty:
            linestrings.append(linestring)
    return MultiLineString(linestrings)


def serialize_polygon(geom: Polygon) -> bytearray:
    exterior_coords = geom.exterior.coords
    if not exterior_coords:
        return create_buffer_for_geom(GeometryTypeID.POLYGON, CoordinateType.XY, 8, 0)
    num_coords = len(exterior_coords)
    for interior in geom.interiors:
        num_coords += len(interior.coords)
    bytes_per_coord = len(exterior_coords[0]) * 8
    num_rings_offset = 8 + num_coords * bytes_per_coord
    num_rings = len(geom.interiors) + 1
    size = num_rings_offset + 4 + 4 * num_rings
    coord_type = CoordinateType.type_of(exterior_coords, geom.has_z)
    buffer = create_buffer_for_geom(GeometryTypeID.POLYGON, coord_type, size, num_coords)
    geom_buffer = GeometryBuffer(buffer, coord_type, 8, num_coords)
    geom_buffer.write_polygon(geom)
    return buffer


def deserialize_polygon(geom_buffer: GeometryBuffer) -> Polygon:
    if geom_buffer.num_coords == 0:
        return wkt_loads("POLYGON EMPTY")
    return geom_buffer.read_polygon()


def serialize_multi_polygon(geom: MultiPolygon) -> bytearray:
    polygons = geom.geoms
    if not polygons:
        return create_buffer_for_geom(GeometryTypeID.MULTIPOLYGON, CoordinateType.XY, 8, 0)
    coord_type = CoordinateType.type_of(polygons[0].exterior.coords, geom.has_z)
    bytes_per_coord = CoordinateType.bytes_per_coord(coord_type)
    num_coords = 0
    num_rings = 0
    for polygon in polygons:
        num_exterior_coords = len(polygon.exterior.coords)
        if num_exterior_coords == 0:
            continue
        num_coords += num_exterior_coords
        num_rings += (1 + len(polygon.interiors))
        for interior in polygon.interiors:
            num_coords += len(interior.coords)
    num_polygons_offset = 8 + num_coords * bytes_per_coord
    size = num_polygons_offset + 4 + len(polygons) * 4 + num_rings * 4
    buffer = create_buffer_for_geom(GeometryTypeID.MULTIPOLYGON, coord_type, size, num_coords)
    geom_buffer = GeometryBuffer(buffer, coord_type, 8, num_coords)
    geom_buffer.write_int(len(polygons))
    for polygon in polygons:
        geom_buffer.write_polygon(polygon)
    return buffer


def deserialize_multi_polygon(geom_buffer: GeometryBuffer) -> MultiPolygon:
    if geom_buffer.num_coords == 0:
        return wkt_loads("MULTIPOLYGON EMPTY")
    num_polygons = geom_buffer.read_int()
    polygons = []
    for k in range(0, num_polygons):
        polygon = geom_buffer.read_polygon()
        if not polygon.is_empty:
            polygons.append(polygon)
    return MultiPolygon(polygons)


def serialize_geometry_collection(geom: GeometryCollection) -> bytearray:
    if not isinstance(geom, GeometryCollection):
        # In shapely 1.x, Empty geometries such as empty points, empty polygons etc.
        # have geom_type property evaluated to 'GeometryCollection'. Such geometries are not
        # instances of GeometryCollection and do not have `geom` property.
        return serialize_shapely_1_empty_geom(geom)
    geometries = geom.geoms
    if not geometries:
        return create_buffer_for_geom(GeometryTypeID.GEOMETRYCOLLECTION, CoordinateType.XY, 8, 0)
    num_geometries = len(geometries)
    total_size = 8
    buffers = []
    for geom in geometries:
        buf = serialize(geom)
        buffers.append(buf)
        total_size += aligned_offset(len(buf))
    buffer = create_buffer_for_geom(GeometryTypeID.GEOMETRYCOLLECTION, CoordinateType.XY, total_size, num_geometries)
    offset = 8
    for buf in buffers:
        buffer[offset:(offset + len(buf))] = buf
        offset += aligned_offset(len(buf))
    return buffer


def serialize_shapely_1_empty_geom(geom) -> bytearray:
    if isinstance(geom, Point):
        geom_type = GeometryTypeID.POINT
    elif isinstance(geom, LineString):
        geom_type = GeometryTypeID.LINESTRING
    elif isinstance(geom, Polygon):
        geom_type = GeometryTypeID.POLYGON
    elif isinstance(geom, MultiPoint):
        geom_type = GeometryTypeID.MULTIPOINT
    elif isinstance(geom, MultiLineString):
        geom_type = GeometryTypeID.MULTILINESTRING
    elif isinstance(geom, MultiPolygon):
        geom_type = GeometryTypeID.MULTIPOLYGON
    else:
        raise ValueError("Invalid empty geometry collection object: {}".format(geom))
    return create_buffer_for_geom(geom_type, CoordinateType.XY, 8, 0)


def deserialize_geometry_collection(geom_buffer: GeometryBuffer) -> GeometryCollection:
    num_geometries = geom_buffer.num_coords
    if num_geometries == 0:
        return wkt_loads("GEOMETRYCOLLECTION EMPTY")
    geometries = []
    geom_end_offset = 8
    buffer = geom_buffer.buffer[8:]
    for k in range(0, num_geometries):
        geom, offset = deserialize(buffer)
        geometries.append(geom)
        offset = aligned_offset(offset)
        buffer = buffer[offset:]
        geom_end_offset += offset
    geom_buffer.ints_offset = geom_end_offset
    return GeometryCollection(geometries)
