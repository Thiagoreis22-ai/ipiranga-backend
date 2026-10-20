import math
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from ops_router import AreaIn, EquipmentIn, TagIn, LoopIn, ModeIn, Reading, parse_time


def test_area_code_normalization():
    doc = AreaIn(code=" trat-caldo ", name="Tratamento de Caldo")
    assert doc.code == "TRAT-CALDO"


def test_area_rejects_unsafe_code():
    with pytest.raises(ValidationError):
        AreaIn(code="../../..", name="Tentativa inválida")


def test_reading_rejects_nan_and_infinity():
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            Reading(tag_id="abc", value=value, observed_at="2026-10-20T17:00:00+00:00", quality="GOOD")


def test_timestamp_requires_timezone():
    with pytest.raises(HTTPException) as exc:
        parse_time("2026-10-20T17:00:00")
    assert exc.value.status_code == 422


def test_auto_is_only_requested_mode():
    loop = LoopIn(code="TIC_101", name="Controle cadastrado",
                  area_id="a1", pv_tag_id="tag1", requested_mode="AUTOMATICO")
    assert loop.requested_mode == "AUTOMATICO"
    # Escrita NÃO é parte do modelo de entrada e NÃO existe aqui.
    assert "write_enabled" not in loop.model_fields


def test_reject_invalid_mode():
    with pytest.raises(ValidationError):
        ModeIn(requested_mode="ESCREVER_NO_PLC", justification="Solicitação indevida")


def test_tag_bounds_must_be_finite():
    with pytest.raises(ValidationError):
        TagIn(code="TIC_101", name="Teste", area_id="a1", warning_high=math.inf)
