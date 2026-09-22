"""Saving and deleting.

Two views serve every screen in the module, parameterised by model, form and
page code. They answer JSON, because the drawer stays open while the server
replies -- a person's typing is not thrown away to show them an error.

Three things every write does, in this order:

1. **Check the permission on the write.** Hiding a button is a courtesy; this is
   the control. The legacy only hid buttons, which is why its permissions were
   decorative.
2. **Check the row is in scope.** A company user cannot reach another company's
   row by posting its id.
3. **Do the work in a transaction.**
"""

import json

from django.db import models, transaction
from django.db.models import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.portal.services import has_permission
from core.scoping import scoped_to


def _payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST.dict()


def _translate(data, field_map):
    if not field_map:
        return data
    return {field_map.get(key, key): value for key, value in data.items()}


def _errors(form):
    """Every error at once, labelled the way the drawer labels its fields."""
    out = []
    for name, messages in form.errors.items():
        if name == "__all__":
            label = ""
        else:
            field = form.fields.get(name)
            label = field.label if field and field.label else name.replace("_", " ").title()
        for message in messages:
            out.append({"field": label, "message": message})
    return out


class WriteView(View):
    """Shared plumbing: the permission check and the scoped queryset."""

    model = None
    page_code = None
    scope_field = "company"

    def rows(self):
        return scoped_to(self.model.objects.all(), self.request.user, field=self.scope_field)

    def may(self, action):
        user = getattr(self.request, "user", None)
        return bool(user) and has_permission(user, self.page_code, action)

    def refused(self):
        return JsonResponse(
            {"ok": False, "code": "forbidden",
             "errors": [{"field": "", "message": "You do not have permission to do that."}]},
            status=403,
        )


class EntitySaveView(WriteView):
    """Create when no pk is posted, update when there is one."""

    form_class = None
    noun = "Record"
    # Drawer field id -> model field. The ids come from the wireframe's specs
    # and are also used by show_if/enables, so they are translated here rather
    # than renamed there.
    field_map = {}
    # Masters with no company column (Country, State, Location) are shared
    # reference data; there is nothing to scope them by.
    scoped = True

    def rows(self):
        if not self.scoped:
            return self.model.objects.all()
        return super().rows()

    def after_save(self, instance, data):
        """Rows the drawer collects that the form itself does not own.

        Runs inside the same transaction as the save, so a screen never ends up
        half written. A drawer field with nowhere to go is worse than no field:
        it looks like it was recorded.
        """

    def post(self, request, *args, **kwargs):
        data = _translate(_payload(request), self.field_map)
        pk = data.get("pk") or None

        if not self.may("update" if pk else "create"):
            return self.refused()

        instance = get_object_or_404(self.rows(), pk=pk) if pk else None
        form = self.form_class(data, instance=instance, user=request.user) \
            if _takes_user(self.form_class) else self.form_class(data, instance=instance)

        if not form.is_valid():
            return JsonResponse({"ok": False, "code": "invalid", "errors": _errors(form)}, status=400)

        with transaction.atomic():
            saved = form.save()
            self.after_save(saved, data)

        return JsonResponse({
            "ok": True,
            "pk": saved.pk,
            "message": f"{self.noun} saved.",
        })


class EntityDeleteView(WriteView):
    """Delete when nothing references the row; deactivate and report when
    something does.

    The references come from the database's own ProtectedError -- the real ones,
    not a guess at which tables might point here.
    """

    noun = "Record"
    scoped = True

    def rows(self):
        if not self.scoped:
            return self.model.objects.all()
        return super().rows()

    def post(self, request, pk, *args, **kwargs):
        if not self.may("delete"):
            return self.refused()

        instance = get_object_or_404(self.rows(), pk=pk)
        name = str(instance)

        links = references(instance)
        if not links:
            try:
                with transaction.atomic():
                    instance.delete()
                return JsonResponse({"ok": True, "message": f"{name} deleted."})
            except ProtectedError as blocked:
                # Belt and braces: the database is the final word on this.
                links = _group_by_model(blocked.protected_objects)

        if hasattr(instance, "is_active"):
            instance.is_active = False
            instance.save(update_fields=["is_active"])
        return JsonResponse({
            "ok": False,
            "code": "in_use",
            "title": f"{name} is still in use",
            "subtitle": self.noun,
            "links": links,
            "note": "It has been deactivated instead, so nothing that uses it breaks.",
        }, status=409)


def references(instance):
    """Everywhere this row is still used, as [{'label', 'count'}].

    Asked before deleting rather than after, because ProtectedError only covers
    PROTECT foreign keys. A many-to-many link -- a department sitting in a
    branch's Departments list -- raises nothing at all: Django would quietly
    drop the join rows and the department would vanish from branches that use
    it. That silent case is exactly what a person needs telling about.

    Cascades are deliberately not counted: a branch's working hours belong to
    that branch and should go with it.
    """
    found = []
    for relation in instance._meta.related_objects:
        accessor = relation.get_accessor_name()
        if relation.many_to_many:
            count = getattr(instance, accessor).count()
        elif relation.on_delete is models.PROTECT:
            count = relation.related_model.objects.filter(
                **{relation.field.name: instance}
            ).count()
        else:
            continue
        if count:
            found.append({
                "label": str(relation.related_model._meta.verbose_name_plural).title(),
                "count": count,
            })
    return sorted(found, key=lambda link: link["label"])


def _group_by_model(objects):
    """[{'label': 'Branches', 'count': 3}] from the protected rows."""
    counts = {}
    for obj in objects:
        meta = obj._meta
        counts[meta.verbose_name_plural.title()] = counts.get(meta.verbose_name_plural.title(), 0) + 1
    return [{"label": label, "count": count} for label, count in sorted(counts.items())]


def _takes_user(form_class):
    """Does this form want the signed-in user?

    The whole MRO is searched, not just `form_class.__init__`. A subclass that
    overrides __init__ as `(self, *args, **kwargs)` forwards `user` perfectly
    well, but its own signature does not name it -- and reading only that
    signature made the form silently lose its scoping: no company, no scoped
    dropdowns, and a "Company is required" error the person could do nothing
    about, because that field is hidden from them.
    """
    import inspect

    return any(
        "user" in inspect.signature(klass.__init__).parameters
        for klass in form_class.__mro__
        if "__init__" in vars(klass)
    )
