from apps.company.forms import ScopedModelForm
from apps.fleet.models import Brand


class BrandForm(ScopedModelForm):
    class Meta:
        model = Brand
        fields = [
            "company", "brand_code", "brand_name", "description",
            "manufacturer", "website_link", "is_active",
        ]
        labels = {"brand_code": "Brand Code", "brand_name": "Brand Name"}
