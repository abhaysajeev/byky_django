from django.urls import path

from apps.tracking import views

urlpatterns = [
    path("status/", views.StatusView.as_view(), name="tracking-status"),
    path("alerts/", views.AlertsView.as_view(), name="tracking-alerts"),
    path("alerts/<int:pk>/acknowledge/", views.AcknowledgeView.as_view(), name="tracking-alert-acknowledge"),

    path("bell/feed/", views.BellFeedView.as_view(), name="tracking-bell-feed"),
    path("bell/read/", views.BellReadView.as_view(), name="tracking-bell-read"),

    path("readers/", views.ReadersView.as_view(), name="tracking-readers"),
    path("readers/add/", views.ReaderAddView.as_view(), name="tracking-reader-add"),
    path("readers/<int:pk>/approve/", views.ReaderApproveView.as_view(), name="tracking-reader-approve"),
    path("readers/<int:pk>/edit/", views.ReaderEditView.as_view(), name="tracking-reader-edit"),
    path("readers/<int:pk>/enabled/", views.ReaderToggleView.as_view(), name="tracking-reader-enabled"),
    path("readers/<int:pk>/block/", views.ReaderBlockView.as_view(), name="tracking-reader-block"),
    path("readers/<int:pk>/unblock/", views.ReaderUnblockView.as_view(), name="tracking-reader-unblock"),

    path("antennas/", views.AntennasView.as_view(), name="tracking-antennas"),
    path("antennas/<int:pk>/direction/", views.AntennaDirectionView.as_view(), name="tracking-antenna-direction"),
    path("antennas/<int:pk>/calibration/<slug:action>/", views.CalibrationView.as_view(),
         name="tracking-calibration"),

    path("raw/", views.RawView.as_view(), name="tracking-raw"),
    path("settings/", views.SettingsView.as_view(), name="tracking-settings"),
    path("settings/<int:pk>/save/", views.SettingsSaveView.as_view(), name="tracking-settings-save"),
    path("privileges/", views.TrackingPrivilegeView.as_view(), name="tracking-privileges"),
]
