from apps.company.forms import ScopedModelForm
from apps.fleet.models import Brand, Category


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
