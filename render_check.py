
import django, os
from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.messages.middleware import MessageMiddleware
from core.models import User
from apps.crew.views import DutyRosterListView

u = User.objects.filter(role__isnull=False).first()
print("user:", u, u.company if u else None)

rf = RequestFactory()
request = rf.get("/crew/duty-roster/")
SessionMiddleware(lambda r: None).process_request(request)
request.session.save()
MessageMiddleware(lambda r: None).process_request(request)
request.user = u

try:
    response = DutyRosterListView.as_view()(request)
    response.render()
    print("status", response.status_code)
    html = response.content.decode()
    open("/tmp/duty_roster_render.html", "w").write(html)
    print("LEN", len(html))
except Exception as e:
    import traceback
    traceback.print_exc()
