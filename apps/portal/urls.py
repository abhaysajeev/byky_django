from django.urls import path

from apps.portal import screens, views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),

    # Screens
    path("portal/role/list/", screens.RoleListView.as_view(), name="portal-role-list"),
    path("portal/user/list/", screens.UserListView.as_view(), name="portal-user-list"),

    # Writes
    path("portal/role/save/", screens.RoleSave.as_view(), name="portal-role-save"),
    path("portal/role/<int:pk>/delete/", screens.RoleDelete.as_view(), name="portal-role-delete"),
    path("portal/user/save/", screens.UserSave.as_view(), name="portal-user-save"),
    path("portal/user/<int:pk>/delete/", screens.UserDelete.as_view(), name="portal-user-delete"),
    path("portal/user/<int:pk>/reset-password/", screens.UserResetPassword.as_view(),
         name="portal-user-reset-password"),
    path("portal/user/<int:pk>/unlock/", screens.UserUnlock.as_view(), name="portal-user-unlock"),

    # The privilege grid: one write path, shared by every module's screen.
    path("portal/privileges/add/", screens.PrivilegeAddRole.as_view(), name="portal-privilege-add"),
    path("portal/privileges/remove/", screens.PrivilegeRemoveRole.as_view(),
         name="portal-privilege-remove"),
    path("portal/privileges/save/", screens.PrivilegeSave.as_view(), name="portal-privilege-save"),

    path("", views.home_view, name="home"),
]
