"""Type cleanup preserves domain errors for incomplete external inputs."""

import pytest


@pytest.mark.parametrize("value", [None, True, "1000", 1.5, 0, -1])
@pytest.mark.parametrize("kind", ["indent", "po"])
def test_procurement_rejects_invalid_quantities_with_domain_error(value, kind):
    from services import procurement_service as service

    line = {"item_code": "TEST", "quantity_milli": value, "unit_rate_paise": 100}
    coerce = (
        service._coerce_indent_lines if kind == "indent" else service._coerce_po_lines
    )
    with pytest.raises(service.ProcurementError) as refused:
        coerce([line])
    assert refused.value.code == "bad_quantity"


@pytest.mark.parametrize("kind", ["indent", "po"])
def test_procurement_missing_quantity_is_a_domain_error(kind):
    from services import procurement_service as service

    coerce = (
        service._coerce_indent_lines if kind == "indent" else service._coerce_po_lines
    )
    with pytest.raises(service.ProcurementError) as refused:
        coerce([{"item_code": "TEST", "unit_rate_paise": 100}])
    assert refused.value.code == "bad_quantity"


def test_purchase_order_missing_rate_uses_money_validation():
    from services import procurement_service as service
    from services.money import MoneyError

    with pytest.raises(MoneyError):
        service._coerce_po_lines([{"item_code": "TEST", "quantity_milli": 1000}])


@pytest.mark.parametrize(
    "missing,code", [("weekday", "bad_weekday"), ("slot_minutes", "bad_slot")]
)
@pytest.mark.parametrize("explicit_none", [False, True])
def test_incomplete_clinic_session_has_a_domain_error(missing, code, explicit_none):
    from services import clinic_schedule_service as service

    fields = {
        "weekday": 1,
        "start_time": "09:00",
        "end_time": "12:00",
        "slot_minutes": 15,
    }
    if explicit_none:
        fields[missing] = None
    else:
        del fields[missing]
    with pytest.raises(service.ClinicScheduleError) as refused:
        service._validated_session_fields(fields)
    assert refused.value.code == code
