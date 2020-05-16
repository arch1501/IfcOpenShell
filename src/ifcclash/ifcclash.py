#!python

import collision
import ifcopenshell
import ifcopenshell.geom
import multiprocessing
import numpy as np
import json
import sys
import argparse
import logging


class Mesh:
    faces: []
    vertices: []


class IfcClasher:
    def __init__(self, a_file, b_file, settings):
        self.settings = settings
        self.geom_settings = ifcopenshell.geom.settings()
        self.tolerance = 0.01
        self.a = None
        self.b = None
        self.a_file = a_file
        self.b_file = b_file
        self.clashes = {}
        self.a_meshes = {}
        self.b_meshes = {}

    def clash(self):
        for ab in ['a', 'b']:
            self.settings.logger.info(f'Loading file {ab} ...')
            setattr(self, ab, ifcopenshell.open(getattr(self, f'{ab}_file')))
            self.patch_ifc(ab)
            self.settings.logger.info(f'Purging unnecessary elements {ab} ...')
            self.purge_elements(ab)
            self.settings.logger.info(f'Creating collision manager {ab} ...')
            setattr(self, f'{ab}_cm', collision.CollisionManager())
            self.add_collision_objects(ab)
        results = self.a_cm.in_collision_other(self.b_cm, return_data=True)

        if not results[0]:
            return

        for contact in results[1]:
            a_global_id, b_global_id = contact.names
            a = self.a.by_guid(a_global_id)
            b = self.b.by_guid(b_global_id)
            if contact.raw.penetration_depth < self.tolerance:
                continue
            self.clashes[f'{a_global_id}-{b_global_id}'] = {
                'a_global_id': a_global_id,
                'b_global_id': b_global_id,
                'a_ifc_class': a.is_a(),
                'b_ifc_class': b.is_a(),
                'a_name': a.Name,
                'b_name': b.Name,
                'normal': list(contact.raw.normal),
                'position': list(contact.raw.pos),
                'penetration_depth': contact.raw.penetration_depth
            }

    def export(self):
        with open(self.settings.output, 'w', encoding='utf-8') as clashes_file:
            json.dump(list(self.clashes.values()), clashes_file, indent=4)

    def purge_elements(self, ab):
        # TODO: more filtering abilities
        for element in getattr(self, ab).by_type('IfcSpace'):
            getattr(self, ab).remove(element)

    def add_collision_objects(self, ab):
        self.settings.logger.info('Creating collision data for {}'.format(ab))
        iterator = ifcopenshell.geom.iterator(self.geom_settings, getattr(self, ab), multiprocessing.cpu_count())
        valid_file = iterator.initialize()
        if not valid_file:
            return False
        old_progress = -1
        while True:
            progress = iterator.progress() // 2
            if progress > old_progress:
                print("\r[" + "#" * progress + " " * (50 - progress) + "]", end="")
                old_progress = progress
            self.add_collision_object(ab, iterator.get())
            if not iterator.next():
                break

    def add_collision_object(self, ab, shape):
        if shape is None:
            return
        element = getattr(self, ab).by_id(shape.guid)
        self.settings.logger.info('Creating object {}'.format(element))
        mesh_name = f'mesh-{shape.geometry.id}'
        if mesh_name in getattr(self, f'{ab}_meshes'):
            mesh = getattr(self, f'{ab}_meshes')[mesh_name]
        else:
            mesh = self.create_mesh(shape)
            getattr(self, f'{ab}_meshes')[mesh_name] = mesh

        m = shape.transformation.matrix.data
        mat = np.array(
            [
                [m[0], m[3], m[6], m[9]],
                [m[1], m[4], m[7], m[10]],
                [m[2], m[5], m[8], m[11]],
                [0, 0, 0, 1]
            ]
        )
        mat.transpose()
        getattr(self, f'{ab}_cm').add_object(shape.guid, mesh, mat)

    def create_mesh(self, shape):
        f = shape.geometry.faces
        v = shape.geometry.verts
        mesh = Mesh()
        mesh.vertices = np.array([[v[i], v[i + 1], v[i + 2]]
                 for i in range(0, len(v), 3)])
        mesh.faces = np.array([[f[i], f[i + 1], f[i + 2]]
                 for i in range(0, len(f), 3)])
        return mesh

    def patch_ifc(self, ab):
        project = getattr(self, ab).by_type('IfcProject')[0]
        sites = self.find_decomposed_ifc_class(project, 'IfcSite')
        for site in sites:
            self.patch_placement_to_origin(site)
        buildings = self.find_decomposed_ifc_class(project, 'IfcBuilding')
        for building in buildings:
            self.patch_placement_to_origin(building)

    def find_decomposed_ifc_class(self, element, ifc_class):
        results = []
        rel_aggregates = element.IsDecomposedBy
        if not rel_aggregates:
            return results
        for rel_aggregate in rel_aggregates:
            for part in rel_aggregate.RelatedObjects:
                if part.is_a(ifc_class):
                    results.append(part)
                results.extend(self.find_decomposed_ifc_class(part, ifc_class))
        return results

    def patch_placement_to_origin(self, element):
        element.ObjectPlacement.RelativePlacement.Location.Coordinates = (0., 0., 0.)
        if element.ObjectPlacement.RelativePlacement.Axis:
            element.ObjectPlacement.RelativePlacement.Axis.DirectionRatios = (0., 0., 1.)
        if element.ObjectPlacement.RelativePlacement.RefDirection:
            element.ObjectPlacement.RelativePlacement.RefDirection.DirectionRatios = (1., 0., 0.)


class IfcClashSettings:
    def __init__(self):
        self.logger = None
        self.output = 'clashes.json'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Clashes geometry between two IFC files')
    parser.add_argument(
        'a',
        type=str,
        help='The IFC file containing group A of objects to clash')
    parser.add_argument(
        'b',
        type=str,
        help='The IFC file containing group B of objects to clash')
    parser.add_argument(
        '-o',
        '--output',
        type=str,
        help='The JSON diff file to output. Defaults to clashes.json',
        default='clashes.json')
    args = parser.parse_args()

    settings = IfcClashSettings()
    settings.output = args.output
    settings.logger = logging.getLogger('Clash')
    settings.logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    settings.logger.addHandler(handler)
    ifc_clasher = IfcClasher(args.a, args.b, settings)
    ifc_clasher.clash()
    ifc_clasher.export()