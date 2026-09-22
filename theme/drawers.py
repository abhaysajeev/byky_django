"""Drawer specs for the screens that use the shared byky drawer.

Every create/edit drawer on a converted screen is declared as a spec dict and
rendered by byky/partials/drawer.html -- one template, never forked per screen
(byky-drawer README section 8).

A spec looks like:

    {
      "drawer_id": "drawerCountry",     # DOM id
      "scr_name": "country",            # data-scr-open="country:add|edit"
      "add_label": "Add Country",
      "title_field": "name",            # titles the drawer in edit mode
      "sections": [
        {"title": "", "fields": [ ...field dicts... ]},
      ],
    }

A field's dropdown options usually come from the view context (branches,
categories, employees...), which a static dict cannot hold. So a select carries
`options_from`, the name of a context key, and resolve() copies the spec and
fills each `resolved` list at render time. The specs themselves stay importable,
inspectable data with no Django context bound into them.
"""

import copy


def resolve(spec, context):
    """Return a render-ready copy of `spec` with every select's options filled.

    `options_from` names a context key holding either a list of strings or a
    list of dicts; for dicts, `option_key` says which key to read (default
    "name"). A key the context does not carry resolves to an empty list rather
    than raising -- a drawer with an empty dropdown is a visible, honest gap,
    where a 500 on an unrelated screen is not.

    A select whose choices are fixed by the domain rather than by data --
    Gender, Marital Status -- carries a literal `options` list instead, and is
    resolved straight from it. Without this a literal list rendered as an empty
    dropdown, because the template only ever reads `resolved`. radio and
    checkgroup read `options` directly and are left alone.
    """
    fills_from_options = ("select", "multiselect")
    out = copy.deepcopy(spec)
    for section in out.get("sections", []):
        if section.get("note_from"):
            section["note"] = context.get(section["note_from"]) or ""
        for field in section.get("fields", []):
            src = field.get("options_from")
            if not src:
                if field.get("kind") in fills_from_options and field.get("options"):
                    field["resolved"] = list(field["options"])
                continue
            key = field.get("option_key", "name")
            values = context.get(src) or []
            # A dict source resolves to {id, name}: the option's value is the
            # row's id, so a save matches the row and not its display name.
            # Two companies can each have an "Operations" department, and
            # matching on the label would pick the wrong one.
            field["resolved"] = [
                {"id": v.get("id", ""), "name": v.get(key, "")} if isinstance(v, dict) else v
                for v in values
            ]
    return out


def resolve_all(specs, context):
    """resolve() over a {template_var: spec} mapping."""
    return {name: resolve(spec, context) for name, spec in specs.items()}


def for_user(spec, is_system_user):
    """Drop fields that only a system user chooses.

    A company user never picks a company: it is theirs, the server fills it in,
    and offering the choice would be both noise and a way to write into someone
    else's data. A system user belongs to no company, so they must choose one.

    Mark such a field with "system_only": True.
    """
    if is_system_user:
        return spec
    out = copy.deepcopy(spec)
    for section in out.get("sections", []):
        section["fields"] = [
            field for field in section.get("fields", [])
            if not field.get("system_only")
        ]
    return out


def all_for_user(specs, is_system_user):
    return {name: for_user(spec, is_system_user) for name, spec in specs.items()}
