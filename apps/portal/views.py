"""Web sign-in, sign-out and the landing page.

The templates here are deliberately plain: the real UI shell arrives with the
first screen port (.claude/skills/port-ui/SKILL.md).
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.company import scoping
from apps.portal.auth import LoginRefused, close_session, sign_in_web
from apps.portal.permissions import PagePermissionMixin
from apps.portal.session_models import AppSession, LogoutReason
from core.middleware import SESSION_ID_KEY, SESSION_USER_KEY
from theme.views import ThemedTemplateView


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    # request.user is a lazy object, so test it for truth, never for None.
    if request.user:
        return redirect("home")

    if request.method == "GET":
        return render(request, "portal/login.html")

    username = request.POST.get("username", "")
    password = request.POST.get("password", "")

    # A brand-new session key, so a sign-in never reuses the visitor's old one.
    request.session.cycle_key()

    try:
        user, session = sign_in_web(
            username, password,
            ip_address=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except LoginRefused as refusal:
        return render(
            request,
            "portal/login.html",
            {"error": refusal.message, "username": username},
            status=refusal.status,
        )

    request.session[SESSION_USER_KEY] = user.pk
    request.session[SESSION_ID_KEY] = session.pk
    return redirect(_safe_next(request) or "home")


def _safe_next(request):
    """Where to go after signing in, if the visitor was sent here from a page.

    Only a path on this site. `next` arrives in the query string, so anyone can
    write it into a link -- and without this check, `/login/?next=https://evil`
    would hand a freshly signed-in user straight to someone else's page.
    """
    target = request.GET.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return None


@never_cache
def logout_view(request):
    session_id = request.session.get(SESSION_ID_KEY)
    if session_id:
        session = AppSession.objects.filter(pk=session_id, logged_out_at__isnull=True).first()
        if session:
            close_session(session, LogoutReason.USER_LOGOUT)
    request.session.flush()
    messages.success(request, "You have been signed out.")
    return redirect("login")


@never_cache
def home_view(request):
    """`/` is the dashboard once signed in."""
    if not request.user:
        return redirect("login")
    return redirect("dashboard")


class DashboardView(PagePermissionMixin, ThemedTemplateView):
    """The landing page.

    Every widget the wireframe drew is here. The figures behind them come from
    our own tables, so counts read zero and the charts are empty until the
    modules that own those numbers are built -- nothing is seeded to make the
    page look busy.

    No `page_code` -- deliberately. `general.dashboard` stays registered in
    `page_registry.py` (other consumers, e.g. the app `permissions_for`
    response, still see it), but the web view itself is never permission-gated:
    any signed-in user lands here regardless of what their role's
    `RolePermission` rows say, so a brand-new role with nothing granted yet
    doesn't 403 on its own landing page.
    """

    template_name = "portal/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        branches = scoping.branches_for(self.request.user)
        stations = branches.filter(is_active=True).count()
        points = [
            {
                "name": branch.name,
                "emirate": branch.location.state.name,
                "fleet": 0,
                "lat": float(branch.latitude),
                "lng": float(branch.longitude),
            }
            for branch in branches.select_related("location__state").filter(
                is_active=True, latitude__isnull=False, longitude__isnull=False
            )
        ]

        context.update({
            # Real counts, from the two masters that exist.
            "stations": stations,
            "emirates": scoping.states_for(self.request.user).filter(is_active=True).count(),
            "empty_stations": stations,          # none holds a vehicle yet
            "stocked_stations": 0,
            "deployment_pct": 0,
            "avg_per_station": 0,
            "largest_station": {"name": "", "vehicle_count": 0},
            # Fleet, crew and rental modules are not built, so these are zero
            # rather than invented.
            "vehicles": 0,
            "categories": 0,
            "vehicle_types": 0,
            "electric": 0,
            "standard": 0,
            "electric_pct": 0,
            "standard_pct": 0,
            "employees": 0,
            "sales": {
                "monthly_revenue": 0, "daily_avg": 0, "rentals": 0, "avg_ticket": 0,
                "paid_invoices": 0, "pending_invoices": 0,
                "delta": "0.0%", "paid_pct": 0, "pending_pct": 0,
                "revenue_pct": 0, "rentals_pct": 0, "ticket_pct": 0,
            },
            "top_stations_revenue": [],
            "by_profession": [],
            "invoices": [],
            "map_points": points,
            # Empty arrays, never arrays of zeros: the chart code divides by the
            # series maximum, and an all-zero series would draw NaN geometry.
            "chart_data": {
                "daily_labels": [], "daily_revenue": [],
                "monthly_labels": [], "monthly_revenue": [],
                "revenue_categories": [], "revenue_category_values": [],
                "map_points": points,
                "map_centre": [24.3, 54.5],
                "map_zoom": 7,
            },
        })
        return context
