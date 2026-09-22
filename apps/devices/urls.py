from django.urls import path

from apps.devices import views

urlpatterns = [
    # Screens
    path(
        "approval/",
        views.DeviceApprovalView.as_view(),
        name="devices-device-approval",
    ),
    # Keyed on the registration number, not the primary key: that number is
    # what the tablet displays and the admin is told over the phone.
    path(
        "approval/<int:registration_id>/",
        views.DeviceApprovalDetailView.as_view(),
        name="devices-device-approval-detail",
    ),
    path(
        "mapping/",
        views.DeviceMappingView.as_view(),
        name="devices-device-mapping",
    ),
    path("releases/", views.AppReleaseView.as_view(), name="devices-app-release"),
    path("release-mapping/", views.AppMappingView.as_view(), name="devices-app-mapping"),
    path("settings/", views.DeviceSettingsView.as_view(), name="devices-device-settings"),
    path(
        "privileges/",
        views.DevicePrivilegeView.as_view(),
        name="devices-privileges",
    ),

    # Device Approval actions, keyed on the primary key the screen rows carry.
    path("approval/<int:pk>/approve/", views.DeviceApproveView.as_view(), name="devices-device-approve"),
    path("approval/<int:pk>/reconnect/", views.DeviceReconnectView.as_view(), name="devices-device-reconnect"),
    path("approval/<int:pk>/reject/", views.DeviceRejectView.as_view(), name="devices-device-reject"),
    path("approval/<int:pk>/block/", views.DeviceBlockView.as_view(), name="devices-device-block"),
    path("approval/<int:pk>/unblock/", views.DeviceUnblockView.as_view(), name="devices-device-unblock"),

    # Device Mapping: Add (drawer), Re-map and Close mapping.
    path("mapping/save/", views.DeviceMapSaveView.as_view(), name="devices-mapping-save"),
    path("mapping/<int:pk>/remap/", views.DeviceRemapView.as_view(), name="devices-mapping-remap"),
    path("mapping/<int:pk>/close/", views.DeviceCloseMappingView.as_view(), name="devices-mapping-close"),

    # App Releases: the drawer (create / edit), Withdraw and Restore.
    path("releases/save/", views.ReleaseSaveView.as_view(), name="devices-release-save"),
    path("releases/<int:pk>/withdraw/", views.ReleaseWithdrawView.as_view(), name="devices-release-withdraw"),
    path("releases/<int:pk>/restore/", views.ReleaseRestoreView.as_view(), name="devices-release-restore"),

    # App Mapping: preview before saving, save, and remove one mapping.
    path("release-mapping/preview/", views.MappingPreviewView.as_view(), name="devices-release-mapping-preview"),
    path("release-mapping/save/", views.MappingSaveView.as_view(), name="devices-release-mapping-save"),
    path("release-mapping/<int:pk>/remove/", views.MappingRemoveView.as_view(), name="devices-release-mapping-remove"),

    # Device Settings: the drawer, the logo (its own multipart endpoint and a
    # view that serves the bytes), copy-to-stations, and deactivate.
    path("settings/save/", views.SettingsSaveView.as_view(), name="devices-settings-save"),
    path("settings/<int:pk>/logo/", views.SettingsLogoView.as_view(), name="devices-settings-logo"),
    path("settings/<int:pk>/logo/save/", views.SettingsLogoSaveView.as_view(), name="devices-settings-logo-save"),
    path("settings/<int:pk>/logo/clear/", views.SettingsLogoClearView.as_view(), name="devices-settings-logo-clear"),
    path("settings/<int:pk>/apply/", views.SettingsApplyView.as_view(), name="devices-settings-apply"),
    path("settings/<int:pk>/deactivate/", views.SettingsDeactivateView.as_view(), name="devices-settings-deactivate"),
]
