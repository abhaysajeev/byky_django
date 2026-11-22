"""The A4 tax invoice (client, 22 Nov 2026): the preview's Print button opens
it in a new tab. Same figures as the receipt, same rules; the receipt itself
is untouched."""

from django.utils import timezone

from apps.rental.models import Invoice
from apps.rental.tests import test_invoice_screens as screens
from apps.rental.tests.test_views import revoke
from core.models import User

issue, shop, invoice = screens.issue, screens.shop, screens.invoice


def test_the_a4_invoice_is_a_whole_page_with_the_receipts_figures(client_in, invoice):
    receipt = client_in.get(f"/rental/invoice/{invoice.pk}/receipt/")
    page = client_in.get(f"/rental/invoice/{invoice.pk}/print/")

    assert page.status_code == 200
    html = page.content.decode()
    assert html.lstrip().startswith("<!doctype html>") and "byky-print-a4.css" in html
    assert page.context["bill"] == receipt.context["bill"]          # one source of figures
    for text in ("TAX INVOICE", "100297867200003", invoice.invoice_no, "MO 41", "(10%)",
                 "AED 54.000", "VAT @ 5% (included)", "window.print()"):
        assert text in html, text


def test_print_0_leaves_the_dialog_closed(client_in, invoice):
    html = client_in.get(f"/rental/invoice/{invoice.pk}/print/?print=0").content.decode()
    assert "addEventListener('load'" not in html


def test_the_a4_invoice_needs_read_and_the_company(client_in, world, invoice):
    revoke(client_in, "read", page="rental.invoice")
    assert client_in.get(f"/rental/invoice/{invoice.pk}/print/").status_code == 403


def test_another_companys_invoice_has_no_a4(client_in, world, invoice):
    Invoice.objects.filter(pk=invoice.pk).update(company=world["other"])
    assert client_in.get(f"/rental/invoice/{invoice.pk}/print/").status_code == 404


def test_a_cancelled_invoice_has_no_a4(client_in, invoice):
    Invoice.objects.filter(pk=invoice.pk).update(
        voided_at=timezone.now(), voided_by=User.objects.get(username="sara.k"), void_reason="Mistake")
    assert client_in.get(f"/rental/invoice/{invoice.pk}/print/").status_code == 404


def test_the_preview_is_the_receipt_and_print_opens_the_a4(client_in, invoice):
    receipt = client_in.get(f"/rental/invoice/{invoice.pk}/receipt/").content.decode()
    assert "<html" not in receipt and "Bike Rental Details" in receipt          # unchanged
    detail = client_in.get(f"/rental/invoice/{invoice.pk}/").content.decode()
    assert "Print opens an A4 copy" in detail
    assert "data-invoice-modal-print" in detail and "data-scr-print data-invoice-modal-print" not in detail
