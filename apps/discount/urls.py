from django.urls import path

from apps.discount import views, writes

urlpatterns = [
    path("card-type/list/", views.CardTypeListView.as_view(), name="discount-card-type-list"),
    path("card-type/save/", views.CardTypeSave.as_view(), name="discount-card-type-save"),
    path("card-type/<int:pk>/delete/", views.CardTypeDelete.as_view(), name="discount-card-type-delete"),

    path("card-grade/list/", views.CardGradeListView.as_view(), name="discount-card-grade-list"),
    path("card-grade/save/", views.CardGradeSave.as_view(), name="discount-card-grade-save"),
    path("card-grade/<int:pk>/delete/", views.CardGradeDelete.as_view(), name="discount-card-grade-delete"),

    path("card-discount/list/", views.CardDiscountListView.as_view(), name="discount-card-discount-list"),
    path("card-discount/add/", views.CardDiscountFormView.as_view(), name="discount-card-discount-add"),
    path("card-discount/<int:pk>/edit/", views.CardDiscountFormView.as_view(), name="discount-card-discount-edit"),
    path("card-discount/save/", writes.CardDiscountSave.as_view(), name="discount-card-discount-save"),
    path("card-discount/<int:pk>/delete/", views.CardDiscountDelete.as_view(),
         name="discount-card-discount-delete"),

    path("approval/list/", views.ApprovalListView.as_view(), name="discount-approval-list"),
    path("approval/<uuid:pk>/", views.ApprovalDetailView.as_view(), name="discount-approval-detail"),
    path("approval/<uuid:pk>/approve/", writes.ClaimApprove.as_view(), name="discount-approval-approve"),
    path("approval/<uuid:pk>/reject/", writes.ClaimReject.as_view(), name="discount-approval-reject"),

    path("redemption/list/", views.RedemptionListView.as_view(), name="discount-redemption-list"),
    path("redemption/export/", views.RedemptionExport.as_view(), name="discount-redemption-export"),

    path("privileges/", views.DiscountPrivilegeView.as_view(), name="discount-privileges"),
]
