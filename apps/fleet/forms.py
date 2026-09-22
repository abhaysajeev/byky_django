from apps.company.forms import ScopedModelForm
from apps.fleet.models import Brand, Category, VehicleType


class BrandForm(ScopedModelForm):
    class Meta:
        model = Brand
        fields = [
            "company", "brand_code", "brand_name", "description",
            "manufacturer", "website_link", "is_active",
        ]
        labels = {"brand_code": "Brand Code", "brand_name": "Brand Name"}


class CategoryForm(ScopedModelForm):
    class Meta:
        model = Category
        fields = ["company", "category_code", "category_name", "description", "is_active"]
        labels = {"category_code": "Category Code", "category_name": "Category Name"}


class VehicleTypeForm(ScopedModelForm):
    class Meta:
        model = VehicleType
        fields = [
            "company", "category", "brand", "vehicle_type_code",
            "vehicle_type_name", "description", "is_active",
        ]
        labels = {
            "vehicle_type_code": "Vehicle Type Code", "vehicle_type_name": "Vehicle Type Name",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user is not None:
            from apps.fleet.scoping import brands_for, categories_for

            # Only this company's categories and brands -- ScopedModelForm
            # only scopes the "company" field itself; a cross-referencing FK
            # needs its own queryset restriction, or a company user could
            # post another company's category/brand id and have it accepted.
            self.fields["category"].queryset = categories_for(self.user).filter(is_active=True)
            self.fields["brand"].queryset = brands_for(self.user).filter(is_active=True)
