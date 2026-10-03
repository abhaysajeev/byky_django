"""apps.fare.package_services: saving a whole package."""


import pytest

from apps.fare import package_payload, package_services
from apps.fare.models import Package, PackageFreeItem, PackageItem
from apps.fare.package_services import Invalid, NotFound


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
        "package_code": "PROMO1", "package_name": "Promo", "level": "company", "branch": "", "location": "",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31", "is_active": True,
        "promotion_for": "quantity", "inventory_type": "vehicle_type",
        "lower_value": "1", "upper_value": "5", "promotion_type": "amount",
        "time_slab_applicable": False, "free_item_selectable": False, "free_item_selectable_note": "",
        "free_or_package_price": "free",
        "items": [item("n1", world["monaco"].pk)], "free_items": [], "time_slabs": [],
    }
    values.update(changes)
    return values


def fields(error):
    return [e["field"] for e in error.value.errors]


def messages(error):
    return " ".join(e["message"] for e in error.value.errors)


# -- Create ------------------------------------------------------------------------


def test_a_package_is_saved_approved_for_the_users_company(world, user):
    package = package_services.save_package(user, data(world))
    assert package.company == world["company"]
    assert package.package_code == "PROMO1"
    assert package.created_by == user
    assert package.items.count() == 1


def test_a_branch_package_stores_its_branch(world, user):
    package = package_services.save_package(
        user, data(world, level="branch", branch=world["adc1"].pk))
    assert package.branch == world["adc1"] and package.location is None


def test_a_location_package_stores_its_location(world, user):
    package = package_services.save_package(
        user, data(world, level="location", location=world["location"].pk))
    assert package.location == world["location"] and package.branch is None


def test_quantity_promotion_with_free_items(world, user):
    package = package_services.save_package(user, data(
        world, promotion_type="quantity", free_item_selectable=True,
        items=[item("n1", world["monaco"].pk, value=None)],
        free_items=[free_item("f1", world["berg"].pk)],
    ))
    assert package.items.get().value is None
    assert package.free_items.get().vehicle_type == world["berg"]


def test_time_slab_applicable_stores_slabs(world, user):
    package = package_services.save_package(user, data(
        world, time_slab_applicable=True, time_slabs=[slab("t1", world["monaco"].pk)]))
    row = package.time_slabs.get()
    assert (row.from_time.strftime("%H:%M"), row.to_time.strftime("%H:%M")) == ("10:00", "12:00")


def test_a_vehicle_type_from_another_company_is_refused(world, user):
    with pytest.raises(Invalid) as error:
        package_services.save_package(user, data(world, items=[item("n1", world["their_type"].pk)]))
    assert "Promotion Items" in fields(error)


def test_a_branch_from_another_company_is_refused(world, user):
    with pytest.raises(Invalid) as error:
        package_services.save_package(user, data(world, level="branch", branch=world["theirs"].pk))
    assert "Branch" in fields(error)


def test_at_least_one_promotion_item_is_required(world, user):
    with pytest.raises(Invalid) as error:
        package_services.save_package(user, data(world, items=[]))
    assert "Promotion Items" in fields(error)


def test_dates_out_of_order_are_refused(world, user):
    with pytest.raises(Invalid) as error:
        package_services.save_package(user, data(world, valid_from="2026-12-31", valid_to="2026-01-01"))
    assert "Valid To" in fields(error)


# -- Update -----------------------------------------------------------------------


def saved_with_children(world, user):
    return package_services.save_package(user, data(
        world, promotion_type="quantity", free_item_selectable=True, time_slab_applicable=True,
        items=[item("n1", world["monaco"].pk, value=None), item("n2", world["berg"].pk, value=None)],
        free_items=[free_item("f1", world["monaco"].pk)],
        time_slabs=[slab("t1", world["monaco"].pk)],
    ))


def test_an_update_keeps_ids_and_drops_what_was_removed(world, user):
    package = saved_with_children(world, user)
    sent = package_payload.serialise(package_services.with_children(Package.objects.filter(pk=package.pk)).get())
    kept = sent["items"][0]
    sent["items"] = [kept]                                          # one promotion item removed
    sent["package_name"] = "Promo Updated"

    updated = package_services.save_package(user, sent)
    assert updated.package_name == "Promo Updated"
    assert PackageItem.objects.filter(package=package).count() == 1
    assert PackageItem.objects.get(pk=kept["id"]).package_id == package.pk


def test_switching_off_free_item_selectable_clears_free_items_server_side(world, user):
    package = saved_with_children(world, user)
    sent = package_payload.serialise(package_services.with_children(Package.objects.filter(pk=package.pk)).get())
    sent["free_item_selectable"] = False
    sent["free_items"] = []                                          # the browser hides, but still sends none

    updated = package_services.save_package(user, sent)
    assert updated.free_item_selectable is False
    assert not PackageFreeItem.objects.filter(package=package).exists()


def test_switching_level_clears_the_other_scope_fk(world, user):
    package = package_services.save_package(
        user, data(world, level="branch", branch=world["adc1"].pk))
    sent = package_payload.serialise(package_services.with_children(Package.objects.filter(pk=package.pk)).get())
    sent["level"] = "location"
    sent["location"] = world["location"].pk
    sent["branch"] = world["adc1"].pk                                 # the browser may still send the old one

    updated = package_services.save_package(user, sent)
    assert updated.branch is None and updated.location == world["location"]


def test_an_unknown_pk_is_not_found(world, user):
    sent = data(world)
    sent["pk"] = 999999
    with pytest.raises(NotFound):
        package_services.save_package(user, sent)


def test_a_row_id_from_another_package_is_refused(world, user):
    package = saved_with_children(world, user)
    other = package_services.save_package(user, data(world, package_code="PROMO2"))
    foreign_item = PackageItem.objects.create(package=other, vehicle_type=world["monaco"], package_minutes=45)

    sent = package_payload.serialise(package_services.with_children(Package.objects.filter(pk=package.pk)).get())
    sent["items"][0]["id"] = foreign_item.pk
    sent["items"][0]["key"] = f"i{foreign_item.pk}"
    with pytest.raises(Invalid):
        package_services.save_package(user, sent)
