"""Every contract type survives a JSON round trip unchanged."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil

import mux.contracts
import pytest
from mux.contracts._base import Contract, Tagged
from pydantic import BaseModel

BASES = (Contract, Tagged)


def _origin(model: type[BaseModel]) -> type[BaseModel]:
    return model.__pydantic_generic_metadata__["origin"] or model


def _contract_types() -> set[type[BaseModel]]:
    found: set[type[BaseModel]] = set()
    for info in pkgutil.iter_modules(mux.contracts.__path__, "mux.contracts."):
        module = importlib.import_module(info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Contract) and obj not in BASES and obj.__module__ == info.name:
                found.add(_origin(obj))
    return found


def test_every_contract_type_has_a_sample(samples: tuple[BaseModel, ...]) -> None:
    sampled = {_origin(type(sample)) for sample in samples}
    missing = sorted(t.__name__ for t in _contract_types() - sampled)
    assert not missing, f"add a sample to tests/conftest.py for: {missing}"


def test_json_round_trip(sample: BaseModel) -> None:
    wire = json.dumps(sample.model_dump(mode="json", exclude_unset=True))
    restored = type(sample).model_validate(json.loads(wire))
    assert restored == sample
    assert restored.model_fields_set == sample.model_fields_set
    assert type(sample).model_validate(sample.model_dump()) == sample


def test_contracts_are_frozen(sample: BaseModel) -> None:
    field = next(iter(type(sample).model_fields))
    with pytest.raises(ValueError, match="frozen"):
        setattr(sample, field, None)
