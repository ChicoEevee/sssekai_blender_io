from . import *

def search_env_meshes(env : Environment):
    '''(Partially) Loads the UnityPy Environment for further Mesh processing

    Args:
        env (Environment): UnityPy Environment

    Returns:
        Tuple[List[GameObject], Dict[str,Armature]]: Static Mesh GameObjects and Armatures
    '''
    # Collect all static meshes and skinned meshes's *root transform* object
    # UnityPy does not construct the Bone Hierarchy so we have to do it ourselves
    static_mesh_gameobjects : List[GameObject] = list() # No extra care needed
    transform_roots = []
    for obj in env.objects:
        data = obj.read()
        if obj.type == ClassIDType.GameObject and getattr(data,'m_MeshRenderer',None):
            static_mesh_gameobjects.append(data)
        if obj.type == ClassIDType.Transform:
            if hasattr(data,'m_Children') and not data.m_Father.path_id:
                transform_roots.append(data)
    # Collect all skinned meshes as Armature[s]
    # Note that Mesh maybe reused across Armatures, but we don't care...for now
    armatures = []
    for root in transform_roots:
        armature = Armature(root.m_GameObject.read().m_Name)
        armature.bone_path_hash_tbl = dict()
        armature.bone_name_tbl = dict()
        path_id_tbl = dict() # Only used locally
        def dfs(root : Transform, parent : Bone = None):            
            gameObject = root.m_GameObject.read()
            name = gameObject.m_Name
            # Addtional properties
            # Skinned Mesh Renderer
            if getattr(gameObject,'m_SkinnedMeshRenderer',None):
                armature.skinned_mesh_gameobject = gameObject
            # Complete path. Used for CRC hash later on
            path_from_root = ''
            if parent and parent.global_path:
                path_from_root = parent.global_path + '/' + name
            elif parent:
                path_from_root = name
            # Physics Rb + Collider
            # XXX: Some properties are not implemented yet
            bonePhysics = None
            for component in gameObject.m_Components:
                if component.type == ClassIDType.MonoBehaviour:
                    component = component.read()
                    if component.m_Script:
                        physicsScript = component.m_Script.read()
                        physics = component.read_typetree()
                        phy_type = None                        
                        if physicsScript.name == 'SpringSphereCollider':
                            phy_type = BonePhysicsType.SphereCollider
                        if physicsScript.name == 'SpringCapsuleCollider':
                            phy_type = BonePhysicsType.CapsuleCollider
                        if physicsScript.name == 'SekaiSpringBone':
                            phy_type = BonePhysicsType.SpringBone
                        if physicsScript.name == 'SpringManager':
                            phy_type = BonePhysicsType.SpringManager
                        if phy_type != None:
                            bonePhysics = BonePhysics.from_dict(physics)                            
                            bonePhysics.type = phy_type
                            if 'pivotNode' in physics:
                                bonePhysics.pivot = path_id_tbl[physics['pivotNode']['m_PathID']].name
            bone = Bone(
                name,
                root.m_LocalPosition,
                root.m_LocalRotation,
                root.m_LocalScale,
                parent,
                list(),
                path_from_root,
                None,
                bonePhysics
            )
            path_id_tbl[root.path_id] = bone
            armature.bone_name_tbl[name] = bone
            armature.bone_path_hash_tbl[get_name_hash(path_from_root)] = bone
            if not parent:
                armature.root = bone
            else:
                parent.children.append(bone)
            for child in root.m_Children:
                dfs(child.read(), bone)
        dfs(root)    
        if armature.skinned_mesh_gameobject:
            armatures.append(armature)
    return static_mesh_gameobjects, armatures

def search_env_animations(env : Environment):
    '''Searches the Environment for AnimationClips

    Args:
        env (Environment): UnityPy Environment

    Returns:
        List[AnimationClip]: AnimationClips
    '''
    animations = []
    for asset in env.assets:
        for obj in asset.get_objects():
            data = obj.read()
            if obj.type == ClassIDType.AnimationClip:
                animations.append(data)
    return animations

def import_mesh(name : str, data: Mesh, skinned : bool = False, bone_path_tbl : Dict[str,Bone] = None):
    '''Imports the mesh data into blender.

    Takes care of the following:
    - Vertices (Position + Normal) and indices (Trig Faces)
    - UV Map
    Additonally, for Skinned meshes:
    - Bone Indices + Bone Weights
    - Blend Shape / Shape Keys

    Args:
        name (str): Name for the created Blender object
        data (Mesh): Source UnityPy Mesh data
        skinned (bool, optional): Whether the Mesh has skinning data, i.e. attached to SkinnedMeshRenderer. Defaults to False.

    Returns:
        Tuple[bpy.types.Mesh, bpy.types.Object]: Created mesh and its parent object
    '''
    print('* Importing Mesh', data.name, 'Skinned=', skinned)
    mesh = bpy.data.meshes.new(name=data.name)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bm = bmesh.new()
    vtxFloats = int(len(data.m_Vertices) / data.m_VertexCount)
    normalFloats = int(len(data.m_Normals) / data.m_VertexCount)
    uvFloats = int(len(data.m_UV0) / data.m_VertexCount)
    colorFloats = int(len(data.m_Colors) / data.m_VertexCount)
    # Bone Indices + Bone Weights
    deform_layer = None
    if skinned:
        for boneHash in data.m_BoneNameHashes:
            # boneHash is the CRC32 hash of the full bone path
            # i.e Position/Hips/Spine/Spine1/Spine2/Neck/Head
            group_name = bone_path_tbl[boneHash].name   
            obj.vertex_groups.new(name=group_name)
        deform_layer = bm.verts.layers.deform.new()
        # Animations uses the hash to identify the bone
        # so this has to be stored in the metadata as well
        mesh[KEY_BONE_NAME_HASH_TBL] = json.dumps({k:v.name for k,v in bone_path_tbl.items()},ensure_ascii=False)
    # Vertex position & vertex normal (pre-assign)
    for vtx in range(0, data.m_VertexCount):        
        vert = bm.verts.new(swizzle_vector3(
            data.m_Vertices[vtx * vtxFloats], # x,y,z
            data.m_Vertices[vtx * vtxFloats + 1],
            data.m_Vertices[vtx * vtxFloats + 2]            
        ))
        # Blender always generates normals automatically
        # Custom normals needs a bit more work
        # See below for normals_split... calls
        # XXX why is this flipped?
        vert.normal = swizzle_vector3(
            -1 * data.m_Normals[vtx * normalFloats],
            -1 * data.m_Normals[vtx * normalFloats + 1],
            -1 * data.m_Normals[vtx * normalFloats + 2]
        )
        if deform_layer:
            for i in range(4):
                skin = data.m_Skin[vtx]
                if skin.weight[i] <= 0:
                    break
                vertex_group_index = skin.boneIndex[i]                
                vert[deform_layer][vertex_group_index] = skin.weight[i]
    bm.verts.ensure_lookup_table()
    # Indices
    for idx in range(0, len(data.m_Indices), 3):
        face = bm.faces.new([bm.verts[data.m_Indices[idx + j]] for j in range(3)])
        face.smooth = True
    bm.to_mesh(mesh)
    # UV Map
    uv_layer = mesh.uv_layers.new()
    mesh.uv_layers.active = uv_layer
    for face in mesh.polygons:
        for vtx, loop in zip(face.vertices, face.loop_indices):
            uv_layer.data[loop].uv = (
                data.m_UV0[vtx * uvFloats], 
                data.m_UV0[vtx * uvFloats + 1]
            )
    # Vertex Color
    if colorFloats:
        vertex_color = mesh.color_attributes.new(name='Vertex Color',type='FLOAT_COLOR',domain='POINT')
        for vtx in range(0, data.m_VertexCount):
            color = [data.m_Colors[vtx * colorFloats + i] for i in range(colorFloats)]
            vertex_color.data[vtx].color = color
    # Assign vertex normals
    mesh.create_normals_split()
    normals = [(0,0,0) for l in mesh.loops]
    for i, loop in enumerate(mesh.loops):
        normal = bm.verts[loop.vertex_index].normal
        normal.normalize()
        normals[i] = normal
    mesh.normals_split_custom_set(normals)
    mesh.use_auto_smooth = True   
    # Blend Shape / Shape Keys
    if data.m_Shapes.channels:
        obj.shape_key_add(name="Basis")
        keyshape_hash_tbl = dict()
        for channel in data.m_Shapes.channels:
            shape_key = obj.shape_key_add(name=channel.name)
            keyshape_hash_tbl[channel.nameHash] = channel.name
            for frameIndex in range(channel.frameIndex, channel.frameIndex + channel.frameCount):
                # fullWeight = mesh_data.m_Shapes.fullWeights[frameIndex]
                shape = data.m_Shapes.shapes[frameIndex]
                for morphedVtxIndex in range(shape.firstVertex,shape.firstVertex + shape.vertexCount):
                    morpedVtx = data.m_Shapes.vertices[morphedVtxIndex]
                    targetVtx : bpy.types.ShapeKeyPoint = shape_key.data[morpedVtx.index]
                    targetVtx.co += swizzle_vector(morpedVtx.vertex)                    
        # Like boneHash, do the same thing with blend shapes
        mesh[KEY_SHAPEKEY_NAME_HASH_TBL] = json.dumps(keyshape_hash_tbl,ensure_ascii=False)
    bm.free()      
    return mesh, obj

def import_armature(name : str, data : Armature):
    '''Imports the Armature data generated into blender

    NOTE: Unused bones will not be imported since they have identity transforms and thus
    cannot have their own head-tail vectors. It's worth noting though that they won't affect
    the mesh anyway.

    Args:
        name (str): Armature Object name
        data (Armature): Armature as genereated by previous steps
    
    Returns:
        Tuple[bpy.types.Armature, bpy.types.Object]: Created armature and its parent object
    '''
    armature = bpy.data.armatures.new(name)
    armature.display_type = 'STICK'
    armature.relation_line_position = 'HEAD'

    obj = bpy.data.objects.new(name, armature)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    # HACK: *Seems like* the only useful root bone is 'Position' (which is the root of the actual skeleton)
    bone = data.root.recursive_locate_by_name('Position')
    if bone:
        # Build global transforms           
        bone.calculate_global_transforms()
        # Build bone hierarchy in blender
        for parent, child, _ in bone.dfs_generator():
            ebone = armature.edit_bones.new(child.name)
            ebone.use_local_location = True
            ebone.use_relative_parent = False                
            ebone.use_connect = False
            ebone.use_deform = True
            ebone[KEY_BINDPOSE_TRANS] = [v for v in child.get_blender_local_position()]
            ebone[KEY_BINDPOSE_QUAT] = [v for v in child.get_blender_local_rotation()]
            child.edit_bone = ebone
            # Treat the joints as extremely small bones
            # The same as https://github.com/KhronosGroup/glTF-Blender-IO/blob/2debd75ace303f3a3b00a43e9d7a9507af32f194/addons/io_scene_gltf2/blender/imp/gltf2_blender_node.py#L198
            # TODO: Alternative shapes for bones                                                
            ebone.head = child.global_transform @ Vector((0,0,0))
            ebone.tail = child.global_transform @ Vector((0,1,0))
            ebone.length = 0.01
            ebone.align_roll(child.global_transform @ Vector((0,0,1)) - ebone.head)
            if parent:
                ebone.parent = parent.edit_bone

    return armature, obj

def import_armature_physics_constraints(armature, data : Armature):
    '''Imports the rigid body constraints for the armature

    Args:
        armature (bpy.types.Object): Armature object
        data (Armature): Armature data
    '''
    PIVOT_SIZE = 0.004
    SPHERE_RADIUS_FACTOR = 0.5
    CAPSULE_RADIUS_FACTOR = 1
    CAPSULE_HEIGHT_FACTOR = 1
    SPRINGBONE_RADIUS_FACTOR = 0.5
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='OBJECT')
    bone = data.root.recursive_locate_by_name('Position')

    target_rigid_bodies = dict()

    if bone:
        for parent, child, _ in bone.dfs_generator():            
            if child.physics:
                if child.physics.type & BonePhysicsType.Collider:
                    # Add colliders
                    obj = None
                    if child.physics.type == BonePhysicsType.SphereCollider:
                        bpy.ops.mesh.primitive_uv_sphere_add(radius=child.physics.radius * SPHERE_RADIUS_FACTOR)
                        obj = bpy.context.object
                        obj.name = child.name + '_rigidbody'
                        bpy.ops.rigidbody.object_add()
                        obj.rigid_body.type = 'PASSIVE'
                        obj.rigid_body.collision_shape = 'SPHERE'
                    if child.physics.type == BonePhysicsType.CapsuleCollider:
                        bpy.ops.mesh.primitive_cylinder_add(radius=child.physics.radius * CAPSULE_RADIUS_FACTOR,depth=child.physics.height * CAPSULE_HEIGHT_FACTOR)
                        obj = bpy.context.object
                        obj.name = child.name + '_rigidbody'
                        bpy.ops.rigidbody.object_add()
                        obj.rigid_body.type = 'PASSIVE'
                        obj.rigid_body.collision_shape = 'CAPSULE'
                    if obj:
                        obj.rigid_body.kinematic = True
                        obj.parent = armature
                        obj.parent_bone = child.name
                        obj.parent_type = 'BONE'                    
                if child.physics.type & BonePhysicsType.Bone:
                    # Add bones
                    def get_rb_name(bone_name : str, is_pivot : bool):
                        return f'{armature.name}_{bone_name}_{"pivot" if is_pivot else "target"}_rigidbody'
                    def ensure_bone_rigidbody(bone_name : str, is_pivot : bool, radius : float):
                        fullname = get_rb_name(bone_name, is_pivot)
                        if not fullname in bpy.context.scene.objects:
                            bpy.ops.mesh.primitive_uv_sphere_add(radius=radius)
                            obj = bpy.context.object
                            obj.name = fullname
                            bpy.ops.rigidbody.object_add()
                            obj.rigid_body.collision_shape = 'SPHERE'                            
                            obj.rigid_body.type = 'ACTIVE'       
                            obj.parent = armature
                            if is_pivot:
                                obj.rigid_body.collision_collections[0] = False # Does not collide with anything
                                obj.rigid_body.kinematic = True
                                obj.parent_bone = bone_name
                                obj.parent_type = 'BONE'
                            else:                                
                                obj.rigid_body.collision_collections[0] = True # Collides with selected RBs. see below..
                                obj.rigid_body.kinematic = False
                                # Accessing pose bone
                                bpy.context.view_layer.objects.active = armature
                                bpy.ops.object.mode_set(mode='POSE')
                                pbone : bpy.types.PoseBone = armature.pose.bones[bone_name]
                                # Bind inverse should then be identity
                                global_transform = armature.matrix_world @ pbone.matrix
                                obj.matrix_world = global_transform
                                target_rigid_bodies[obj.name] = obj
                            return obj
                        return bpy.context.scene.objects[fullname]
                    if child.physics.type == BonePhysicsType.SpringBone:
                        pivot = ensure_bone_rigidbody(child.physics.pivot, True, PIVOT_SIZE)
                        target = ensure_bone_rigidbody(child.name, False, child.physics.radius * SPRINGBONE_RADIUS_FACTOR)                        
                        # A joint per relationship
                        joint = bpy.data.objects.new("SpringBoneJoint", None)
                        joint.empty_display_size = 0.1
                        joint.empty_display_type = 'ARROWS'
                        # Joint follows the pivot
                        bpy.context.collection.objects.link(joint)
                        joint.parent = pivot
                        bpy.context.view_layer.objects.active = joint    
                        # Add the constraint
                        bpy.ops.rigidbody.constraint_add(type='GENERIC_SPRING')
                        ct = joint.rigid_body_constraint
                        ct.use_limit_lin_x = True
                        ct.use_limit_lin_y = True
                        ct.use_limit_lin_z = True
                        # No linear movement
                        ct.limit_lin_x_lower = 0
                        ct.limit_lin_x_upper = 0
                        ct.limit_lin_y_lower = 0
                        ct.limit_lin_y_upper = 0
                        ct.limit_lin_z_lower = 0
                        ct.limit_lin_z_upper = 0
                        # Angular movement per physics data
                        # Note that the axis are swapped
                        ct.use_limit_ang_x = False
                        ct.use_limit_ang_y = True
                        ct.use_limit_ang_z = True
                        ct.limit_ang_y_lower = math.radians(child.physics.zAngleLimits.min)
                        ct.limit_ang_y_upper = math.radians(child.physics.zAngleLimits.max)
                        ct.limit_ang_z_lower = math.radians(child.physics.yAngleLimits.min)
                        ct.limit_ang_z_upper = math.radians(child.physics.yAngleLimits.max)
                        # Spring damping effect
                        # XXX: These are not going to be accurate
                        ct.use_spring_ang_x = True
                        ct.use_spring_ang_y = True
                        ct.use_spring_ang_z = True                        
                        ct.spring_stiffness_y = ct.spring_stiffness_z = child.physics.angularStiffness
                        # Link the objects!
                        joint.rigid_body_constraint.object1 = pivot
                        joint.rigid_body_constraint.object2 = target
                        # Add the bone constraint
                        bpy.context.view_layer.objects.active = armature
                        bpy.ops.object.mode_set(mode='POSE')
                        pbone : bpy.types.PoseBone = armature.pose.bones[child.name]
                        ct = pbone.constraints.new('COPY_TRANSFORMS')
                        ct.target = target

                    pass
    if target_rigid_bodies:
        def set_no_collision(obj, parent_obj):
            joint = bpy.data.objects.new("NoCollisionJoint", None)
            joint.empty_display_size = 0.1
            joint.empty_display_type = 'ARROWS'
            # Joint follows the pivot
            bpy.context.collection.objects.link(joint)    
            joint.parent = parent_obj                                
            bpy.context.view_layer.objects.active = joint
            bpy.ops.rigidbody.constraint_add(type='GENERIC_SPRING') # Without limits. This acts as a dummy constraint
            ct = joint.rigid_body_constraint
            ct.object1 = obj
            ct.object2 = parent_obj
        rbs = list(target_rigid_bodies.values())
        for i in range(len(rbs)):
            for j in range(i+1, len(rbs)):
                set_no_collision(rbs[i], rbs[j])                

def import_texture(name : str, data : Texture2D):
    '''Imports Texture2D assets into blender

    Args:
        name (str): asset name
        data (Texture2D): source texture

    Returns:
        bpy.types.Image: Created image
    '''
    with tempfile.NamedTemporaryFile(suffix='.bmp',delete=False) as temp:
        print('* Saving Texture', name, 'to', temp.name)
        data.image.save(temp)
        temp.close()
        img = bpy.data.images.load(temp.name, check_existing=True)        
        img.name = name
        print('* Imported Texture', name)
        return img

def load_sssekai_shader_blend():
    if not 'SekaiShaderChara' in bpy.data.materials or not 'SekaiShaderScene' in bpy.data.materials:
        print('! SekaiShader not loaded. Importing from source.')
        with bpy.data.libraries.load(SHADER_BLEND_FILE, link=False) as (data_from, data_to):
            data_to.materials = data_from.materials
            print('! Loaded shader blend file.')

def make_material_texture_node(material , ppTexture):
    texCoord = material.node_tree.nodes.new('ShaderNodeTexCoord')
    uvRemap = material.node_tree.nodes.new('ShaderNodeMapping')
    uvRemap.inputs[1].default_value[0] = ppTexture.m_Offset.X
    uvRemap.inputs[1].default_value[1] = ppTexture.m_Offset.Y
    uvRemap.inputs[3].default_value[0] = ppTexture.m_Scale.X
    uvRemap.inputs[3].default_value[1] = ppTexture.m_Scale.Y
    texture : Texture2D = ppTexture.m_Texture.read()
    texNode = material.node_tree.nodes.new('ShaderNodeTexImage')
    texNode.image = import_texture(texture.name, texture)
    material.node_tree.links.new(texCoord.outputs['UV'], uvRemap.inputs['Vector'])
    material.node_tree.links.new(uvRemap.outputs['Vector'], texNode.inputs['Vector'])
    return texNode

def import_character_material(name : str,data : Material):
    '''Imports Material assets for Characters into blender. 
    
    Args:
        name (str): material name
        data (Material): UnityPy Material

    Returns:
        bpy.types.Material: Created material        
    '''
    load_sssekai_shader_blend()
    material = bpy.data.materials["SekaiShaderChara"].copy()
    material.name = name
    sekaiShader = material.node_tree.nodes['Group']
    textures = data.m_SavedProperties.m_TexEnvs
    if '_MainTex' in textures:
        mainTex = make_material_texture_node(material, textures['_MainTex'])
        material.node_tree.links.new(mainTex.outputs['Color'], sekaiShader.inputs[0])
    if '_ShadowTex' in textures:
        shadowTex = make_material_texture_node(material, textures['_ShadowTex'])
        material.node_tree.links.new(shadowTex.outputs['Color'], sekaiShader.inputs[1])
    if '_ValueTex' in textures:
        valueTex = make_material_texture_node(material, textures['_ValueTex'])
        material.node_tree.links.new(valueTex.outputs['Color'], sekaiShader.inputs[2])
    return material

def import_scene_material(name : str,data : Material):
    '''Imports Material assets for Non-Character (i.e. Stage) into blender. 
    
    Args:
        name (str): material name
        data (Material): UnityPy Material

    Returns:
        bpy.types.Material: Created material        
    '''
    load_sssekai_shader_blend()
    material = bpy.data.materials["SekaiShaderScene"].copy()
    material.name = name
    sekaiShader = material.node_tree.nodes['Group']
    textures = data.m_SavedProperties.m_TexEnvs
    if '_MainTex' in textures:
        mainTex = make_material_texture_node(material, textures['_MainTex'])
        material.node_tree.links.new(mainTex.outputs['Color'], sekaiShader.inputs[0])
    if '_LightMapTex' in textures:
        lightMapTex = make_material_texture_node(material, textures['_LightMapTex'])
        material.node_tree.links.new(lightMapTex.outputs['Color'], sekaiShader.inputs[1])
    return material
