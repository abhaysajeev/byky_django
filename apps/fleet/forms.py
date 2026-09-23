from apps.company.forms import ScopedModelForm
from apps.fleet.models import UOM, Asset, AssetType, Brand, Category, VehicleType


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
            "vehicle_type_name", "description", "tax_percentage", "other_tax", "is_active",
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


class UOMForm(ScopedModelForm):
    class Meta:
        model = UOM
        fields = ["company", "uom_code", "uom_name", "description", "is_active"]
        labels = {"uom_code": "UOM Code", "uom_name": "UOM Name"}


class AssetTypeForm(ScopedModelForm):
    class Meta:
        model = AssetType
        fields = ["company", "asset_type_code", "asset_type_name", "description", "is_active"]
        labels = {"asset_type_code": "Asset Type Code", "asset_type_name": "Asset Type"}


class AssetForm(ScopedModelForm):
    class Meta:
        model = Asset
        fields = [
            "company", "asset_code", "asset_type", "brand", "serial_no", "manufacturer",
            "custodian", "supplier", "purchase_invoice_no", "description",
            "warranty_from_date", "warranty_to_date", "branch", "is_active",
        ]
        labels = {
            "asset_code": "Asset Code", "serial_no": "Serial No",
            "purchase_invoice_no": "Purchase Invoice No",
            "warranty_from_date": "Warranty From Date", "warranty_to_date": "Warranty To Date",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user is not None:
            from apps.company.scoping import branches_for
            from apps.crew.scoping import employees_for
            from apps.fleet.scoping import asset_types_for, brands_for

            # Only this company's rows -- ScopedModelForm only scopes the
            # "company" field itself; every cross-referencing FK needs its
            # own queryset restriction, or a company user could post another
            # company's asset type/brand/employee/branch id and have it
            # accepted (same fix VehicleTypeForm needed for category/brand).
            self.fields["asset_type"].queryset = asset_types_for(self.user).filter(is_active=True)
            self.fields["brand"].queryset = brands_for(self.user).filter(is_active=True)
            self.fields["custodian"].queryset = employees_for(self.user).filter(is_active=True)
            self.fields["branch"].queryset = branches_for(self.user).filter(is_active=True)
