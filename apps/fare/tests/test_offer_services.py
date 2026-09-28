"""apps.fare.offer_services: saving a whole offer."""


import pytest

from apps.fare import offer_payload, offer_services
from apps.fare.models import Offer, OfferFreeItem, OfferItem
from apps.fare.offer_services import Invalid, NotFound


def item(key, vehicle_type, package=30, value="10.00"):
    row = {"key": key, "vehicle_type": vehicle_type, "package_minutes": package}
    if value is not None:
        row["value"] = value
    return row


def free_item(key, vehicle_type, package=30, value="1"):
    return {"key": key, "vehicle_type": vehicle_type, "package_minutes": package, "value": value}


def slab(key, vehicle_type, package=30, value="1", **extra):
    return {"key": key, "vehicle_type": vehicle_type, "package_minutes": package, "value": value,
           "date_mode": "all_dates", "day": "all_days", "from": "10:00", "to": "12:00", **extra}


def data(world, **changes):
    values = {
        "offer_code": "PROMO1", "offer_name": "Promo", "level": "company", "branch": "", "location": "",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31", "is_active": True,
        "promotion_for": "quantity", "inventory_type": "vehicle_type",
        "lower_value": "1", "upper_value": "5", "promotion_type": "amount",
        "time_slab_applicable": False, "free_item_selectable": False, "free_item_selectable_note": "",
        "free_or_offer_price": "free",
        "items": [item("n1", world["monaco"].pk)], "free_items": [], "time_slabs": [],
    }
    values.update(changes)
    return values


def fields(error):
    return [e["field"] for e in error.value.errors]


def messages(error):
    return " ".join(e["message"] for e in error.value.errors)


# -- Create ------------------------------------------------------------------------


def test_an_offer_is_saved_approved_for_the_users_company(world, user):
    offer = offer_services.save_offer(user, data(world))
    assert offer.company == world["company"]
    assert offer.offer_code == "PROMO1"
    assert offer.created_by == user
    assert offer.items.count() == 1


def test_a_branch_offer_stores_its_branch(world, user):
    offer = offer_services.save_offer(
        user, data(world, level="branch", branch=world["adc1"].pk))
    assert offer.branch == world["adc1"] and offer.location is None


def test_a_location_offer_stores_its_location(world, user):
    offer = offer_services.save_offer(
        user, data(world, level="location", location=world["location"].pk))
    assert offer.location == world["location"] and offer.branch is None


def test_quantity_promotion_with_free_items(world, user):
    offer = offer_services.save_offer(user, data(
        world, promotion_type="quantity", free_item_selectable=True,
        items=[item("n1", world["monaco"].pk, value=None)],
        free_items=[free_item("f1", world["berg"].pk)],
    ))
    assert offer.items.get().value is None
    assert offer.free_items.get().vehicle_type == world["berg"]


def test_time_slab_applicable_stores_slabs(world, user):
    offer = offer_services.save_offer(user, data(
        world, time_slab_applicable=True, time_slabs=[slab("t1", world["monaco"].pk)]))
    row = offer.time_slabs.get()
    assert (row.from_time.strftime("%H:%M"), row.to_time.strftime("%H:%M")) == ("10:00", "12:00")


def test_a_vehicle_type_from_another_company_is_refused(world, user):
    with pytest.raises(Invalid) as error:
        offer_services.save_offer(user, data(world, items=[item("n1", world["their_type"].pk)]))
    assert "Promotion Items" in fields(error)


def test_a_branch_from_another_company_is_refused(world, user):
    with pytest.raises(Invalid) as error:
        offer_services.save_offer(user, data(world, level="branch", branch=world["theirs"].pk))
    assert "Branch" in fields(error)


def test_at_least_one_promotion_item_is_required(world, user):
    with pytest.raises(Invalid) as error:
        offer_services.save_offer(user, data(world, items=[]))
    assert "Promotion Items" in fields(error)


def test_dates_out_of_order_are_refused(world, user):
    with pytest.raises(Invalid) as error:
        offer_services.save_offer(user, data(world, valid_from="2026-12-31", valid_to="2026-01-01"))
    assert "Valid To" in fields(error)


# -- Update -----------------------------------------------------------------------


def saved_with_children(world, user):
    return offer_services.save_offer(user, data(
        world, promotion_type="quantity", free_item_selectable=True, time_slab_applicable=True,
        items=[item("n1", world["monaco"].pk, value=None), item("n2", world["berg"].pk, value=None)],
        free_items=[free_item("f1", world["monaco"].pk)],
        time_slabs=[slab("t1", world["monaco"].pk)],
    ))


def test_an_update_keeps_ids_and_drops_what_was_removed(world, user):
    offer = saved_with_children(world, user)
    sent = offer_payload.serialise(offer_services.with_children(Offer.objects.filter(pk=offer.pk)).get())
    kept = sent["items"][0]
    sent["items"] = [kept]                                          # one promotion item removed
    sent["offer_name"] = "Promo Updated"

    updated = offer_services.save_offer(user, sent)
    assert updated.offer_name == "Promo Updated"
    assert OfferItem.objects.filter(offer=offer).count() == 1
    assert OfferItem.objects.get(pk=kept["id"]).offer_id == offer.pk


def test_switching_off_free_item_selectable_clears_free_items_server_side(world, user):
    offer = saved_with_children(world, user)
    sent = offer_payload.serialise(offer_services.with_children(Offer.objects.filter(pk=offer.pk)).get())
    sent["free_item_selectable"] = False
    sent["free_items"] = []                                          # the browser hides, but still sends none

    updated = offer_services.save_offer(user, sent)
    assert updated.free_item_selectable is False
    assert not OfferFreeItem.objects.filter(offer=offer).exists()


def test_switching_level_clears_the_other_scope_fk(world, user):
    offer = offer_services.save_offer(
        user, data(world, level="branch", branch=world["adc1"].pk))
    sent = offer_payload.serialise(offer_services.with_children(Offer.objects.filter(pk=offer.pk)).get())
    sent["level"] = "location"
    sent["location"] = world["location"].pk
    sent["branch"] = world["adc1"].pk                                 # the browser may still send the old one

    updated = offer_services.save_offer(user, sent)
    assert updated.branch is None and updated.location == world["location"]


def test_an_unknown_pk_is_not_found(world, user):
    sent = data(world)
    sent["pk"] = 999999
    with pytest.raises(NotFound):
        offer_services.save_offer(user, sent)


def test_a_row_id_from_another_offer_is_refused(world, user):
    offer = saved_with_children(world, user)
    other = offer_services.save_offer(user, data(world, offer_code="PROMO2"))
    foreign_item = OfferItem.objects.create(offer=other, vehicle_type=world["monaco"], package_minutes=45)

    sent = offer_payload.serialise(offer_services.with_children(Offer.objects.filter(pk=offer.pk)).get())
    sent["items"][0]["id"] = foreign_item.pk
    sent["items"][0]["key"] = f"i{foreign_item.pk}"
    with pytest.raises(Invalid):
        offer_services.save_offer(user, sent)
