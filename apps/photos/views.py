"""A photo on a web screen: /photos/<id>/<size>/ -- signed in, with read on
the screen that shows that kind of photo (services.WEB_PAGES), in a company
the user sees. Anything else is 404, so a guessed id tells nothing."""

from django.http import Http404
from django.shortcuts import redirect
from django.views import View

from apps.photos import services
from apps.photos.models import Photo


class PhotoView(View):
    def get(self, request, pk, size):
        user = getattr(request, "user", None)
        if not user:
            return redirect(f"/login/?next={request.path}")
        photo = Photo.objects.filter(pk=pk).first()
        if size not in services.SIZES or photo is None or not services.can_view_web(user, photo):
            raise Http404("No such photo.")
        return services.serve(photo, size)
