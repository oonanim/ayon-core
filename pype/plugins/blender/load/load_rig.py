"""Load a rig asset in Blender."""

import logging
from pathlib import Path
from pprint import pformat
from typing import Dict, List, Optional

from avalon import api, blender
import bpy
import pype.hosts.blender.plugin as plugin

logger = logging.getLogger("pype").getChild("blender").getChild("load_rig")


class BlendRigLoader(plugin.AssetLoader):
    """Load rigs from a .blend file.

    Because they come from a .blend file we can simply link the collection that
    contains the model. There is no further need to 'containerise' it.
    """

    families = ["rig"]
    representations = ["blend"]

    label = "Link Rig"
    icon = "code-fork"
    color = "orange"

    def _remove(self, objects, lib_container):

        for obj in objects:
            if obj.type == 'ARMATURE':
                bpy.data.armatures.remove(obj.data)
            elif obj.type == 'MESH':
                bpy.data.meshes.remove(obj.data)

        for child in bpy.data.collections[lib_container].children:
            bpy.data.collections.remove(child)

        bpy.data.collections.remove(bpy.data.collections[lib_container])

    def prepare_data(self, data, container_name):
        name = data.name
        data = data.make_local()
        data.name = f"{name}:{container_name}"

    def _process(self, libpath, lib_container, container_name, action):
        relative = bpy.context.preferences.filepaths.use_relative_paths
        with bpy.data.libraries.load(
            libpath, link=True, relative=relative
        ) as (_, data_to):
            data_to.collections = [lib_container]

        scene = bpy.context.scene

        scene.collection.children.link(bpy.data.collections[lib_container])

        rig_container = scene.collection.children[lib_container].make_local()
        rig_container.name = container_name

        meshes = []
        armatures = [
            obj for obj in rig_container.objects if obj.type == 'ARMATURE']

        objects_list = []

        for child in rig_container.children:
            self.prepare_data(child, container_name)
            meshes.extend( child.objects )

        # Link meshes first, then armatures.
        # The armature is unparented for all the non-local meshes,
        # when it is made local.
        for obj in meshes + armatures:
            self.prepare_data(obj, container_name)
            self.prepare_data(obj.data, container_name)

            if not obj.get(blender.pipeline.AVALON_PROPERTY):
                obj[blender.pipeline.AVALON_PROPERTY] = dict()

            avalon_info = obj[blender.pipeline.AVALON_PROPERTY]
            avalon_info.update({"container_name": container_name})

            if obj.type == 'ARMATURE' and action is not None:
                obj.animation_data.action = action

        rig_container.pop(blender.pipeline.AVALON_PROPERTY)

        bpy.ops.object.select_all(action='DESELECT')

        return rig_container

    def process_asset(
        self, context: dict, name: str, namespace: Optional[str] = None,
        options: Optional[Dict] = None
    ) -> Optional[List]:
        """
        Arguments:
            name: Use pre-defined name
            namespace: Use pre-defined namespace
            context: Full parenthood of representation to load
            options: Additional settings dictionary
        """

        libpath = self.fname
        asset = context["asset"]["name"]
        subset = context["subset"]["name"]
        lib_container = plugin.asset_name(
            asset, subset
        )
        namespace = namespace or plugin.asset_namespace(
            asset, subset
        )
        container_name = plugin.asset_name(
            asset, subset, namespace
        )

        container = bpy.data.collections.new(lib_container)
        blender.pipeline.containerise_existing(
            container,
            name,
            namespace,
            context,
            self.__class__.__name__,
        )

        container_metadata = container.get(
            blender.pipeline.AVALON_PROPERTY)

        container_metadata["libpath"] = libpath
        container_metadata["lib_container"] = lib_container

        obj_container = self._process(
            libpath, lib_container, container_name, None)

        container_metadata["obj_container"] = obj_container

        # Save the list of objects in the metadata container
        container_metadata["objects"] = obj_container.all_objects

        nodes = list(container.objects)
        nodes.append(container)
        self[:] = nodes
        return nodes

    def update(self, container: Dict, representation: Dict):
        """Update the loaded asset.

        This will remove all objects of the current collection, load the new
        ones and add them to the collection.
        If the objects of the collection are used in another collection they
        will not be removed, only unlinked. Normally this should not be the
        case though.

        Warning:
            No nested collections are supported at the moment!
        """
        collection = bpy.data.collections.get(
            container["objectName"]
        )
        libpath = Path(api.get_representation_path(representation))
        extension = libpath.suffix.lower()

        logger.info(
            "Container: %s\nRepresentation: %s",
            pformat(container, indent=2),
            pformat(representation, indent=2),
        )

        assert collection, (
            f"The asset is not loaded: {container['objectName']}"
        )
        assert not (collection.children), (
            "Nested collections are not supported."
        )
        assert libpath, (
            "No existing library file found for {container['objectName']}"
        )
        assert libpath.is_file(), (
            f"The file doesn't exist: {libpath}"
        )
        assert extension in plugin.VALID_EXTENSIONS, (
            f"Unsupported file: {libpath}"
        )

        collection_metadata = collection.get(
            blender.pipeline.AVALON_PROPERTY)
        collection_libpath = collection_metadata["libpath"]
        objects = collection_metadata["objects"]
        lib_container = collection_metadata["lib_container"]
        obj_container = collection_metadata["obj_container"]

        normalized_collection_libpath = (
            str(Path(bpy.path.abspath(collection_libpath)).resolve())
        )
        normalized_libpath = (
            str(Path(bpy.path.abspath(str(libpath))).resolve())
        )
        logger.debug(
            "normalized_collection_libpath:\n  %s\nnormalized_libpath:\n  %s",
            normalized_collection_libpath,
            normalized_libpath,
        )
        if normalized_collection_libpath == normalized_libpath:
            logger.info("Library already loaded, not updating...")
            return

        # Get the armature of the rig
        armatures = [obj for obj in objects if obj.type == 'ARMATURE']
        assert(len(armatures) == 1)

        action = armatures[0].animation_data.action

        self._remove(objects, obj_container)

        obj_container = self._process(
            str(libpath), lib_container, collection.name, action)

        # Save the list of objects in the metadata container
        collection_metadata["obj_container"] = obj_container
        collection_metadata["objects"] = obj_container.all_objects
        collection_metadata["libpath"] = str(libpath)
        collection_metadata["representation"] = str(representation["_id"])

        bpy.ops.object.select_all(action='DESELECT')

    def remove(self, container: Dict) -> bool:
        """Remove an existing container from a Blender scene.

        Arguments:
            container (avalon-core:container-1.0): Container to remove,
                from `host.ls()`.

        Returns:
            bool: Whether the container was deleted.

        Warning:
            No nested collections are supported at the moment!
        """

        collection = bpy.data.collections.get(
            container["objectName"]
        )
        if not collection:
            return False
        assert not (collection.children), (
            "Nested collections are not supported."
        )

        collection_metadata = collection.get(
            blender.pipeline.AVALON_PROPERTY)
        objects = collection_metadata["objects"]
        obj_container = collection_metadata["obj_container"]

        self._remove(objects, obj_container)

        bpy.data.collections.remove(collection)

        return True
