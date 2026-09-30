"""Forms for the Card Type and Card Grade drawers. Card Discount has its own
page and payload (apps/discount/payload.py)."""

from apps.company.forms import ScopedModelForm
from apps.discount.models import CardGrade, CardType
from apps.discount.scoping import card_types_for


class AuditedForm(ScopedModelForm):
    """Stamps who created and who last changed the row -- the generic save
    does not."""

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.user is not None:
            if instance.pk is None:
                instance.created_by = self.user
            instance.modified_by = self.user
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class CardTypeForm(AuditedForm):
    class Meta:
        model = CardType
        fields = ["company", "code", "name", "is_active"]
        labels = {"code": "Card Type Code", "name": "Card Type Name"}


class CardGradeForm(AuditedForm):
    class Meta:
        model = CardGrade
        fields = ["company", "card_type", "code", "name", "is_active"]
        labels = {"card_type": "Card Type", "code": "Grade Code", "name": "Grade Name"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user is not None:
            self.fields["card_type"].queryset = card_types_for(self.user).filter(is_active=True)

    def clean(self):
        cleaned = super().clean()
        company, card_type = cleaned.get("company"), cleaned.get("card_type")
        if company and card_type and card_type.company_id != company.pk:
            self.add_error("card_type", "Choose a card type of this company.")
        return cleaned
