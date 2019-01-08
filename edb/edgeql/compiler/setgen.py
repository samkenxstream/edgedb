#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""EdgeQL set compilation functions."""


import contextlib
import copy
import typing

from edb import errors

from edb.common import parsing

from edb.ir import ast as irast
from edb.ir import typeutils as irtyputils
from edb.ir import utils as irutils

from edb.schema import abc as s_abc
from edb.schema import links as s_links
from edb.schema import name as s_name
from edb.schema import nodes as s_nodes
from edb.schema import objtypes as s_objtypes
from edb.schema import pointers as s_pointers
from edb.schema import pseudo as s_pseudo
from edb.schema import sources as s_sources
from edb.schema import types as s_types
from edb.schema import utils as s_utils

from edb.edgeql import ast as qlast
from edb.edgeql import parser as qlparser

from . import astutils
from . import context
from . import dispatch
from . import inference
from . import pathctx
from . import schemactx
from . import stmtctx
from . import typegen


PtrDir = s_pointers.PointerDirection


def new_set(*, stype: s_types.Type, ctx: context.ContextLevel,
            **kwargs) -> irast.Set:
    """Create a new ir.Set instance with given attributes.

    Absolutely all ir.Set instances must be created using this
    constructor.
    """
    typeref = irtyputils.type_to_typeref(ctx.env.schema, stype)
    ir_set = irast.Set(typeref=typeref, **kwargs)
    ctx.env.set_types[ir_set] = stype
    return ir_set


def new_empty_set(*, stype: typing.Optional[s_types.Type]=None, alias: str,
                  ctx: context.ContextLevel) -> irast.Set:
    if stype is None:
        path_id_scls = s_pseudo.Any.create()
    else:
        path_id_scls = stype

    typename = s_name.Name(module='__derived__', name=alias)
    path_id = irast.PathId.from_type(ctx.env.schema, path_id_scls,
                                     typename=typename)
    ir_set = irast.EmptySet(path_id=path_id)
    ctx.env.set_types[ir_set] = stype
    return ir_set


def update_set_type(ir_set: irast.Set, stype: s_types.Type, *,
                    ctx: context.ContextLevel) -> irast.Set:
    ir_set.typeref = irtyputils.type_to_typeref(ctx.env.schema, stype)
    ctx.env.set_types[ir_set] = stype
    return ir_set


def get_set_type(
        ir_set: irast.Set, *,
        ctx: context.ContextLevel) -> s_types.Type:
    return ctx.env.set_types[ir_set]


def new_set_from_set(
        ir_set: irast.Set, *,
        preserve_scope_ns: bool=False,
        path_id: typing.Optional[irast.PathId]=None,
        stype: typing.Optional[s_types.Type]=None,
        ctx: context.ContextLevel) -> irast.Set:
    """Create a new ir.Set from another ir.Set.

    The new Set inherits source Set's scope, schema item, expression,
    and, if *preserve_scope_ns* is set, path_id.  If *preserve_scope_ns*
    is False, the new Set's path_id will be namespaced with the currently
    active scope namespace.
    """
    if path_id is None:
        path_id = ir_set.path_id
    if not preserve_scope_ns:
        path_id = path_id.merge_namespace(ctx.path_id_namespace)
    if stype is None:
        stype = get_set_type(ir_set, ctx=ctx)
    result = new_set(
        path_id=path_id,
        path_scope_id=ir_set.path_scope_id,
        stype=stype,
        expr=ir_set.expr,
        ctx=ctx
    )
    result.rptr = ir_set.rptr
    return result


def compile_path(expr: qlast.Path, *, ctx: context.ContextLevel) -> irast.Set:
    """Create an ir.Set representing the given EdgeQL path expression."""
    anchors = ctx.anchors

    path_tip = None

    if expr.partial:
        if ctx.partial_path_prefix is not None:
            path_tip = ctx.partial_path_prefix
        else:
            raise errors.QueryError(
                'could not resolve partial path ',
                context=expr.context)

    extra_scopes = {}
    computables = []
    path_sets = []

    for i, step in enumerate(expr.steps):
        if isinstance(step, qlast.Source):
            # 'self' can only appear as the starting path label
            # syntactically and is a known anchor
            path_tip = anchors[step.__class__]

        elif isinstance(step, qlast.Subject):
            # '__subject__' can only appear as the starting path label
            # syntactically and is a known anchor
            path_tip = anchors[step.__class__]

        elif isinstance(step, qlast.ObjectRef):
            if i > 0:
                raise RuntimeError(
                    'unexpected ObjectRef as a non-first path item')

            refnode = None

            if not step.module and step.name not in ctx.aliased_views:
                # Check if the starting path label is a known anchor
                refnode = anchors.get(step.name)

            if refnode is not None:
                path_tip = new_set_from_set(
                    refnode, preserve_scope_ns=True, ctx=ctx)
            else:
                stype = schemactx.get_schema_type(
                    step, item_types=(s_objtypes.ObjectType,), ctx=ctx)

                if (stype.get_view_type(ctx.env.schema) is not None and
                        stype.get_name(ctx.env.schema) not in ctx.view_nodes):
                    # This is a schema-level view, as opposed to
                    # a WITH-block or inline alias view.
                    stype = stmtctx.declare_view_from_schema(stype, ctx=ctx)

                path_tip = class_set(stype, ctx=ctx)
                view_set = ctx.view_sets.get(stype)
                if view_set is not None:
                    path_tip = new_set_from_set(view_set, ctx=ctx)
                    path_scope = ctx.path_scope_map.get(view_set)
                    extra_scopes[path_tip] = path_scope.copy()

                view_scls = ctx.class_view_overrides.get(stype.id)
                if view_scls is not None:
                    update_set_type(path_tip, view_scls, ctx=ctx)

        elif isinstance(step, qlast.Ptr):
            # Pointer traversal step
            ptr_expr = step
            ptr_target = None

            direction = (ptr_expr.direction or
                         s_pointers.PointerDirection.Outbound)
            if ptr_expr.target:
                # ... link [IS Target]
                ptr_target = schemactx.get_schema_type(
                    ptr_expr.target.maintype, ctx=ctx)
                if not isinstance(ptr_target, s_objtypes.ObjectType):
                    raise errors.QueryError(
                        f'invalid type filter operand: '
                        f'{ptr_target.get_name(ctx.env.schema)} '
                        f'is not an object type',
                        context=ptr_expr.target.context)

            ptr_name = ptr_expr.ptr.name

            if ptr_expr.type == 'property':
                # Link property reference; the source is the
                # link immediately preceding this step in the path.
                source = irtyputils.ptrcls_from_ptrref(
                    path_tip.rptr.ptrref, schema=ctx.env.schema)
            else:
                source = get_set_type(path_tip, ctx=ctx)

            with ctx.newscope(fenced=True, temporary=True) as subctx:
                if isinstance(source, s_abc.Tuple):
                    path_tip = tuple_indirection_set(
                        path_tip, source=source, ptr_name=ptr_name,
                        source_context=step.context, ctx=subctx)

                else:
                    path_tip = ptr_step_set(
                        path_tip, source=source, ptr_name=ptr_name,
                        direction=direction, ptr_target=ptr_target,
                        ignore_computable=True,
                        source_context=step.context, ctx=subctx)

                    ptrcls = irtyputils.ptrcls_from_ptrref(
                        path_tip.rptr.ptrref, schema=ctx.env.schema)
                    if _is_computable_ptr(ptrcls, ctx=ctx):
                        computables.append(path_tip)

        else:
            # Arbitrary expression
            if i > 0:
                raise RuntimeError(
                    'unexpected expression as a non-first path item')

            with ctx.newscope(fenced=True, temporary=True) as subctx:
                path_tip = ensure_set(
                    dispatch.compile(step, ctx=subctx), ctx=subctx)

                if path_tip.path_id.is_type_indirection_path():
                    scope_set = path_tip.rptr.source
                else:
                    scope_set = path_tip

                extra_scopes[scope_set] = subctx.path_scope

        for key_path_id in path_tip.path_id.iter_weak_namespace_prefixes():
            mapped = ctx.view_map.get(key_path_id)
            if mapped is not None:
                path_tip = new_set(
                    path_id=mapped.path_id,
                    stype=get_set_type(path_tip, ctx=ctx),
                    expr=mapped.expr,
                    rptr=mapped.rptr,
                    ctx=ctx)
                break

        path_sets.append(path_tip)

    path_tip.context = expr.context
    pathctx.register_set_in_scope(path_tip, ctx=ctx)

    for ir_set in computables:
        scope = ctx.path_scope.find_descendant(ir_set.path_id)
        if scope is None:
            # The path is already in the scope, no point
            # in recompiling the computable expression.
            continue

        with ctx.new() as subctx:
            subctx.path_scope = scope
            comp_ir_set = computable_ptr_set(ir_set.rptr, ctx=subctx)
            i = path_sets.index(ir_set)
            if i != len(path_sets) - 1:
                path_sets[i + 1].rptr.source = comp_ir_set
            else:
                path_tip = comp_ir_set
            path_sets[i] = comp_ir_set

    for ir_set, scope in extra_scopes.items():
        node = ctx.path_scope.find_descendant(ir_set.path_id)
        if node is None:
            # The path portion not being a descendant means
            # that is is already present in the scope above us,
            # along with the view scope.
            continue

        fuse_scope_branch(ir_set, node, scope, ctx=ctx)
        if ir_set.path_scope_id is None:
            pathctx.assign_set_scope(ir_set, node, ctx=ctx)

    return path_tip


def fuse_scope_branch(
        ir_set: irast.Set, parent: irast.ScopeTreeNode,
        branch: irast.ScopeTreeNode, *,
        ctx: context.ContextLevel) -> None:
    if parent.path_id is None:
        parent.attach_subtree(branch)
    else:
        if branch.path_id is None and len(branch.children) == 1:
            target_branch = next(iter(branch.children))
        else:
            target_branch = branch

        if parent.path_id == target_branch.path_id:
            new_root = irast.new_scope_tree()
            for child in tuple(target_branch.children):
                new_root.attach_child(child)

            parent.attach_subtree(new_root)
        else:
            parent.attach_subtree(branch)


def ptr_step_set(
        path_tip: irast.Set, *,
        source: s_sources.Source,
        ptr_name: str,
        direction: PtrDir,
        ptr_target: typing.Optional[s_nodes.Node]=None,
        source_context: parsing.ParserContext,
        ignore_computable: bool=False,
        ctx: context.ContextLevel) -> irast.Set:
    ptrcls = resolve_ptr(
        source, ptr_name, direction,
        target=ptr_target, source_context=source_context,
        ctx=ctx)

    target = ptrcls.get_far_endpoint(ctx.env.schema, direction)

    path_tip = extend_path(
        path_tip, ptrcls, direction, target,
        ignore_computable=ignore_computable, ctx=ctx)

    if ptr_target is not None and target != ptr_target:
        path_tip = class_indirection_set(
            path_tip, ptr_target, optional=False, ctx=ctx)

    return path_tip


def resolve_ptr(
        near_endpoint: s_sources.Source,
        pointer_name: str,
        direction: s_pointers.PointerDirection,
        target: typing.Optional[s_nodes.Node]=None, *,
        source_context: typing.Optional[parsing.ParserContext]=None,
        ctx: context.ContextLevel) -> s_pointers.Pointer:

    ptr = None

    if isinstance(near_endpoint, s_sources.Source):
        ctx.env.schema, ptr = near_endpoint.resolve_pointer(
            ctx.env.schema,
            pointer_name,
            direction=direction,
            look_in_children=False,
            far_endpoint=target)

        if ptr is None:
            if isinstance(near_endpoint, s_links.Link):
                msg = (f'{near_endpoint.get_displayname(ctx.env.schema)} '
                       f'has no property {pointer_name!r}')
                if target:
                    msg += f'of type {target.get_name(ctx.env.schema)!r}'

            elif direction == s_pointers.PointerDirection.Outbound:
                msg = (f'{near_endpoint.get_displayname(ctx.env.schema)} '
                       f'has no link or property {pointer_name!r}')
                if target:
                    msg += f'of type {target.get_name(ctx.env.schema)!r}'

            else:
                nep_name = near_endpoint.get_displayname(ctx.env.schema)
                path = f'{nep_name}.{direction}{pointer_name}'
                if target:
                    path += f'[IS {target.get_displayname(ctx.env.schema)}]'
                msg = f'{path!r} does not resolve to any known path'

            err = errors.InvalidReferenceError(msg, context=source_context)

            if direction == s_pointers.PointerDirection.Outbound:
                near_enpoint_pointers = near_endpoint.get_pointers(
                    ctx.env.schema)
                s_utils.enrich_schema_lookup_error(
                    err, pointer_name, modaliases=ctx.modaliases,
                    item_types=(s_pointers.Pointer,),
                    collection=near_enpoint_pointers.objects(ctx.env.schema),
                    schema=ctx.env.schema
                )

            raise err

    else:
        if direction == s_pointers.PointerDirection.Outbound:
            bptr = schemactx.get_schema_ptr(pointer_name, ctx=ctx)
            schema_cls = ctx.env.schema.get('schema::ScalarType')
            if bptr.get_shortname(ctx.env.schema) == 'std::__type__':
                ctx.env.schema, ptr = bptr.derive(
                    ctx.env.schema, near_endpoint, schema_cls)

    if ptr is None:
        # Reference to a property on non-object
        msg = 'invalid property reference on a primitive type expression'
        raise errors.InvalidReferenceError(msg, context=source_context)

    return ptr


def extend_path(
        source_set: irast.Set,
        ptrcls: s_pointers.Pointer,
        direction: PtrDir=PtrDir.Outbound,
        target: typing.Optional[s_nodes.Node]=None, *,
        ignore_computable: bool=False,
        force_computable: bool=False,
        unnest_fence: bool=False,
        same_computable_scope: bool=False,
        ctx: context.ContextLevel) -> irast.Set:
    """Return a Set node representing the new path tip."""

    if ptrcls.is_link_property(ctx.env.schema):
        src_path_id = source_set.path_id.ptr_path()
    else:
        if direction != s_pointers.PointerDirection.Inbound:
            source = ptrcls.get_near_endpoint(ctx.env.schema, direction)
            stype = get_set_type(source_set, ctx=ctx)
            if not stype.issubclass(ctx.env.schema, source):
                # Polymorphic link reference
                source_set = class_indirection_set(
                    source_set, source, optional=True, ctx=ctx)

        src_path_id = source_set.path_id

    if target is None:
        target = ptrcls.get_far_endpoint(ctx.env.schema, direction)
    path_id = pathctx.extend_path_id(
        src_path_id,
        ptrcls=ptrcls, direction=direction, target=target,
        ns=ctx.path_id_namespace, ctx=ctx)

    target_set = new_set(stype=target, path_id=path_id, ctx=ctx)

    ptr = irast.Pointer(
        source=source_set,
        target=target_set,
        direction=direction,
        ptrref=path_id.rptr(),
    )

    target_set.rptr = ptr

    if (not ignore_computable and _is_computable_ptr(
            ptrcls, force_computable=force_computable, ctx=ctx)):
        target_set = computable_ptr_set(
            ptr, unnest_fence=unnest_fence,
            same_computable_scope=same_computable_scope, ctx=ctx)

    return target_set


def _is_computable_ptr(
        ptrcls, *,
        force_computable: bool=False,
        ctx: context.ContextLevel) -> bool:
    try:
        qlexpr = ctx.source_map[ptrcls][0]
    except KeyError:
        pass
    else:
        return qlexpr is not None

    if ptrcls.is_pure_computable(ctx.env.schema):
        return True

    if force_computable and ptrcls.get_default(ctx.env.schema) is not None:
        return True


def tuple_indirection_set(
        path_tip: irast.Set, *,
        source: s_sources.Source,
        ptr_name: str,
        source_context: parsing.ParserContext,
        ctx: context.ContextLevel) -> irast.Set:

    el_name = ptr_name
    el_norm_name = source.normalize_index(ctx.env.schema, el_name)
    el_type = source.get_subtype(ctx.env.schema, el_name)

    path_id = pathctx.get_tuple_indirection_path_id(
        path_tip.path_id, el_norm_name, el_type, ctx=ctx)
    expr = irast.TupleIndirection(
        expr=path_tip, name=el_norm_name, path_id=path_id,
        context=source_context)

    return generated_set(expr, ctx=ctx)


def class_indirection_set(
        source_set: irast.Set,
        target_scls: s_nodes.Node, *,
        optional: bool,
        ctx: context.ContextLevel) -> irast.Set:

    poly_set = new_set(stype=target_scls, ctx=ctx)
    rptr = source_set.rptr
    if (rptr is not None
            and rptr.ptrref.dir_cardinality is irast.Cardinality.MANY):
        cardinality = irast.Cardinality.MANY
    else:
        cardinality = irast.Cardinality.ONE
    poly_set.path_id = pathctx.get_type_indirection_path_id(
        source_set.path_id, target_scls, optional=optional,
        cardinality=cardinality, ctx=ctx)

    ptr = irast.TypeIndirectionPointer(
        source=source_set,
        target=poly_set,
        ptrref=poly_set.path_id.rptr(),
        direction=poly_set.path_id.rptr_dir(),
        optional=optional,
    )

    poly_set.rptr = ptr

    return poly_set


def class_set(
        stype: s_nodes.Node, *,
        path_id: typing.Optional[irast.PathId]=None,
        ctx: context.ContextLevel) -> irast.Set:

    if path_id is None:
        path_id = pathctx.get_path_id(stype, ctx=ctx)
    return new_set(path_id=path_id, stype=stype, ctx=ctx)


def generated_set(
        expr: irast.Base, path_id: typing.Optional[irast.PathId]=None, *,
        typehint: typing.Optional[s_types.Type]=None,
        ctx: context.ContextLevel) -> irast.Set:
    if typehint is not None:
        ql_typeref = s_utils.typeref_to_ast(ctx.env.schema, typehint)
        ir_typeref = typegen.ql_typeref_to_ir_typeref(ql_typeref, ctx=ctx)
    else:
        ir_typeref = None

    alias = ctx.aliases.get('expr')
    return new_expression_set(
        expr, path_id, alias=alias, typehint=ir_typeref, ctx=ctx)


def get_expression_path_id(
        t: s_types.Type, alias: str, *,
        ctx: context.ContextLevel) -> irast.PathId:
    typename = s_name.Name(module='__derived__', name=alias)
    return pathctx.get_path_id(t, typename=typename, ctx=ctx)


def new_expression_set(
        ir_expr, path_id=None, alias=None,
        typehint: typing.Optional[irast.TypeRef]=None, *,
        ctx: context.ContextLevel) -> irast.Set:
    if typehint is not None and irutils.is_empty(ir_expr):
        update_set_type(ir_expr, typehint, ctx=ctx)

    result_type = inference.infer_type(ir_expr, ctx.env)

    if path_id is None:
        path_id = getattr(ir_expr, 'path_id', None)

        if not path_id:
            if alias is None:
                raise ValueError('either path_id or alias are required')
            path_id = get_expression_path_id(result_type, alias, ctx=ctx)

    return new_set(
        path_id=path_id,
        stype=result_type,
        expr=ir_expr,
        context=ir_expr.context,
        ctx=ctx
    )


def scoped_set(
        expr: irast.Base, *,
        typehint: typing.Optional[s_types.Type]=None,
        path_id: typing.Optional[irast.PathId]=None,
        force_reassign: bool=False,
        ctx: context.ContextLevel) -> irast.Set:

    if not isinstance(expr, irast.Set):
        ir_set = generated_set(expr, typehint=typehint,
                               path_id=path_id, ctx=ctx)
        pathctx.assign_set_scope(ir_set, ctx.path_scope, ctx=ctx)
    else:
        if typehint is not None:
            ir_set = ensure_set(expr, typehint=typehint,
                                path_id=path_id, ctx=ctx)
        else:
            ir_set = expr

        if ir_set.path_scope_id is None or force_reassign:
            if ctx.path_scope.find_child(ir_set.path_id) and path_id is None:
                # Protect from scope recursion in the common case by
                # wrapping the set into a subquery.
                ir_set = generated_set(
                    ensure_stmt(ir_set, ctx=ctx), typehint=typehint, ctx=ctx)

            pathctx.assign_set_scope(ir_set, ctx.path_scope, ctx=ctx)

    return ir_set


def ensure_set(
        expr: irast.Base, *,
        typehint: typing.Optional[s_types.Type]=None,
        path_id: typing.Optional[irast.PathId]=None,
        ctx: context.ContextLevel) -> irast.Set:
    if not isinstance(expr, irast.Set):
        expr = generated_set(expr, typehint=typehint, path_id=path_id, ctx=ctx)

    stype = get_set_type(expr, ctx=ctx)
    if (isinstance(expr, irast.EmptySet)
            and stype is None
            and typehint is not None):
        inference.amend_empty_set_type(expr, typehint, env=ctx.env)
        stype = get_set_type(expr, ctx=ctx)

    if (typehint is not None and
            not stype.implicitly_castable_to(typehint, ctx.env.schema)):
        raise errors.QueryError(
            f'expecting expression of type '
            f'{typehint.get_name(ctx.env.schema)}, '
            f'got {stype.get_name(ctx.env.schema)}',
            context=expr.context
        )
    return expr


def ensure_stmt(expr: irast.Base, *, ctx: context.ContextLevel) -> irast.Stmt:
    if not isinstance(expr, irast.Stmt):
        expr = irast.SelectStmt(
            result=ensure_set(expr, ctx=ctx),
            implicit_wrapper=True,
        )
    return expr


def computable_ptr_set(
        rptr: irast.Pointer, *,
        unnest_fence: bool=False,
        same_computable_scope: bool=False,
        ctx: context.ContextLevel) -> irast.Set:
    """Return ir.Set for a pointer defined as a computable."""
    ptrcls = irtyputils.ptrcls_from_ptrref(rptr.ptrref, schema=ctx.env.schema)
    source_set = rptr.source
    source_scls = get_set_type(source_set, ctx=ctx)
    # process_view() may generate computable pointer expressions
    # in the form "self.linkname".  To prevent infinite recursion,
    # self must resolve to the parent type of the view NOT the view
    # type itself.  Similarly, when resolving computable link properties
    # make sure that we use ptrcls.derived_from.
    if source_scls.is_view(ctx.env.schema):
        source_set = new_set_from_set(
            source_set, preserve_scope_ns=True, ctx=ctx)
        update_set_type(
            source_set, source_scls.peel_view(ctx.env.schema), ctx=ctx)
        source_set.shape = []

        if source_set.rptr is not None:
            schema = ctx.env.schema
            source_rptrref = source_set.rptr.ptrref
            source_rptrcls = irtyputils.ptrcls_from_ptrref(
                source_rptrref, schema=schema)
            derived_from = source_rptrcls.get_derived_from(schema)
            if (derived_from is not None and
                    not derived_from.generic(schema) and
                    derived_from.get_derived_from(schema) is not None and
                    ptrcls.is_link_property(schema)):
                source_set.rptr.ptrref = irtyputils.ptrref_from_ptrcls(
                    source_ref=source_rptrref.dir_source,
                    target_ref=source_rptrref.dir_target,
                    direction=source_rptrref.direction,
                    parent_ptr=source_rptrref.parent_ptr,
                    ptrcls=derived_from,
                    schema=schema,
                )

                stmtctx.ensure_ptrref_cardinality(
                    derived_from, source_set.rptr.ptrref, ctx=ctx)

    try:
        qlexpr, qlctx, inner_source_path_id, path_id_ns = \
            ctx.source_map[ptrcls]
    except KeyError:
        ptrcls_default = ptrcls.get_default(ctx.env.schema)
        if not ptrcls_default:
            ptrcls_sn = ptrcls.get_shortname(ctx.env.schema)
            raise ValueError(
                f'{ptrcls_sn!r} is not a computable pointer')

        qlexpr = astutils.ensure_qlstmt(qlparser.parse(ptrcls_default.text))
        qlctx = None
        inner_source_path_id = None
        path_id_ns = None

    if qlctx is None:
        # Schema-level computable, completely detached context
        newctx = ctx.detached
    else:
        newctx = _get_computable_ctx(
            rptr=rptr,
            source=source_set,
            source_scls=source_scls,
            inner_source_path_id=inner_source_path_id,
            path_id_ns=path_id_ns,
            same_scope=same_computable_scope,
            qlctx=qlctx,
            ctx=ctx)

    if ptrcls.is_link_property(ctx.env.schema):
        source_path_id = rptr.source.path_id.ptr_path()
    else:
        source_path_id = rptr.target.path_id.src_path()

    with newctx() as subctx:
        subctx.view_scls = ptrcls.get_target(ctx.env.schema)
        subctx.view_rptr = context.ViewRPtr(
            source_scls, ptrcls=ptrcls, rptr=rptr)
        subctx.anchors[qlast.Source] = source_set
        subctx.empty_result_type_hint = ptrcls.get_target(ctx.env.schema)

        if isinstance(qlexpr, qlast.Statement) and unnest_fence:
            subctx.stmt_metadata[qlexpr] = context.StatementMetadata(
                is_unnest_fence=True)

        comp_ir_set = dispatch.compile(qlexpr, ctx=subctx)

    pending_cardinality = ctx.pending_cardinality.get(ptrcls)
    if pending_cardinality is not None and not pending_cardinality.from_parent:
        comp_ir_set_copy = copy.copy(comp_ir_set)
        stmtctx.get_pointer_cardinality_later(
            ptrcls=ptrcls, irexpr=comp_ir_set_copy,
            specified_card=pending_cardinality.specified_cardinality,
            source_ctx=pending_cardinality.source_ctx,
            ctx=ctx)

        def _check_cardinality(ctx):
            if ptrcls.singular(ctx.env.schema):
                stmtctx.enforce_singleton_now(comp_ir_set_copy, ctx=ctx)

        stmtctx.at_stmt_fini(_check_cardinality, ctx=ctx)

    path_id = pathctx.extend_path_id(
        source_path_id,
        ptrcls=ptrcls,
        direction=s_pointers.PointerDirection.Outbound,
        target=ptrcls.get_target(ctx.env.schema),
        ns=ctx.path_id_namespace,
        ctx=ctx)

    update_set_type(comp_ir_set, ptrcls.get_target(ctx.env.schema), ctx=ctx)
    comp_ir_set.path_id = path_id
    comp_ir_set.rptr = rptr

    rptr.target = comp_ir_set

    return comp_ir_set


def _get_computable_ctx(
        *,
        rptr: irast.Pointer,
        source: irast.Set,
        source_scls: s_nodes.Node,
        inner_source_path_id: irast.PathId,
        path_id_ns: typing.Optional[irast.WeakNamespace],
        same_scope: bool,
        qlctx: context.ContextLevel,
        ctx: context.ContextLevel) -> typing.ContextManager:
    @contextlib.contextmanager
    def newctx():
        with ctx.new() as subctx:
            subctx.class_view_overrides = {}
            subctx.partial_path_prefix = None

            subctx.modaliases = qlctx.modaliases.copy()
            subctx.aliased_views = qlctx.aliased_views.new_child()
            source_stype = get_set_type(source, ctx=ctx)

            if source_scls.is_view(ctx.env.schema):
                scls_name = source_stype.get_name(ctx.env.schema)
                subctx.aliased_views[scls_name] = None
            subctx.source_map = qlctx.source_map.copy()
            subctx.view_nodes = qlctx.view_nodes.copy()
            subctx.view_sets = qlctx.view_sets.copy()
            subctx.view_map = qlctx.view_map.new_child()

            source_scope = pathctx.get_set_scope(rptr.source, ctx=ctx)
            if source_scope and source_scope.namespaces:
                subctx.path_id_namespace |= source_scope.namespaces

            if path_id_ns is not None:
                subctx.path_id_namespace |= {path_id_ns}

            subctx.pending_stmt_own_path_id_namespace = {
                irast.WeakNamespace(ctx.aliases.get('ns')),
            }

            if path_id_ns is not None and same_scope:
                subctx.pending_stmt_own_path_id_namespace.add(path_id_ns)

            subns = subctx.pending_stmt_full_path_id_namespace = \
                set(subctx.pending_stmt_own_path_id_namespace)

            self_view = ctx.view_sets.get(source_stype)
            if self_view:
                if self_view.path_id.namespace:
                    subns.update(self_view.path_id.namespace)
                inner_path_id = self_view.path_id.merge_namespace(
                    subctx.path_id_namespace | subns)
            else:
                if source.path_id.namespace:
                    subns.update(source.path_id.namespace)

                if inner_source_path_id is not None:
                    # The path id recorded in the source map may
                    # contain namespaces referring to a temporary
                    # scope subtree used by `process_view()`.
                    # Since we recompile the computable expression
                    # using the current path id namespace, the
                    # original source path id needs to be fixed.
                    inner_path_id = inner_source_path_id \
                        .strip_namespace(qlctx.path_id_namespace) \
                        .merge_namespace(subctx.path_id_namespace)
                else:
                    inner_path_id = pathctx.get_path_id(
                        source_stype, ctx=subctx)

                inner_path_id = inner_path_id.merge_namespace(subns)

            remapped_source = new_set_from_set(rptr.source, ctx=ctx)
            remapped_source.rptr = rptr.source.rptr
            subctx.view_map[inner_path_id] = remapped_source
            yield subctx

    return newctx
