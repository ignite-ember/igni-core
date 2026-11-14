"""The schema's types as Pydantic validators, for the host's own checks.

The SDKs get generated classes (:mod:`.codegen`). The host needs the
same types at runtime to check what an extension sends — a parameter of
the wrong shape is refused with :attr:`~.spec.ErrorCode.INVALID` before
any of igni is asked — and, in tests, to check that what the host
answers is what the schema promised.
"""

from __future__ import annotations

from typing import Any, ForwardRef, Optional

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.spec import HostSchema, TypeExpr


class SchemaTypes:
    """Validators for type expressions, built once per schema."""

    def __init__(self, schema: HostSchema) -> None:
        self._schema = schema
        self._models: dict[str, type[BaseModel]] = {}
        self._adapters: dict[str, TypeAdapter[Any]] = {}
        # Named types being built now: one that is reached again while its
        # own fields are built is a type that names itself.
        self._building: list[str] = []
        self._forward = False

    def python_type(self, expression: str) -> Any:
        return self._build(TypeExpr.parse(expression))

    def adapter(self, expression: str) -> TypeAdapter[Any]:
        found = self._adapters.get(expression)
        if found is None:
            found = TypeAdapter(self.python_type(expression))
            self._adapters[expression] = found
        return found

    def validate(self, expression: str, value: Any) -> Any:
        """``value`` as the expression describes it, or :class:`ValidationError`."""
        return self.adapter(expression).validate_python(value)

    def validate_plain(self, expression: str, value: Any) -> Any:
        """``value`` validated as the expression, then back to plain JSON data."""
        return plain(self.validate(expression, value))

    def check(self, expression: str, value: Any) -> str | None:
        """``None`` when ``value`` fits; otherwise why not."""
        try:
            self.validate(expression, value)
        except ValidationError as exc:
            return str(exc)
        return None

    def _build(self, expr: TypeExpr) -> Any:
        if expr.kind == "scalar":
            return {
                "str": str,
                "int": int,
                "float": float,
                "bool": bool,
                "None": type(None),
                "json": Any,
            }[expr.name]
        if expr.kind == "named":
            return self._model(expr.name)
        assert expr.item is not None
        inner = self._build(expr.item)
        if expr.kind == "list":
            return list[inner]  # type: ignore[valid-type]
        if expr.kind == "dict":
            return dict[str, inner]  # type: ignore[valid-type]
        return Optional[inner]  # noqa: UP045 — built at runtime from a value

    def _model(self, name: str) -> type[BaseModel]:
        found = self._models.get(name)
        if found is not None:
            return found
        if name in self._building:
            self._forward = True
            return ForwardRef(name)
        spec = self._schema.type(name)
        if spec is None:
            raise KeyError(name)
        self._building.append(name)
        try:
            fields: dict[str, Any] = {}
            for f in spec.fields:
                annotation = self._build(TypeExpr.parse(f.type))
                if f.optional:
                    fields[f.name] = (annotation, None)
                else:
                    fields[f.name] = (annotation, ...)
            model = create_model(  # type: ignore[call-overload]
                name,
                __config__=ConfigDict(extra="allow", strict=False),
                **fields,
            )
        finally:
            self._building.pop()
        self._models[name] = model
        if self._forward and not self._building:
            # The names left open while building are known now.
            self._forward = False
            for built in self._models.values():
                built.model_rebuild(force=True, _types_namespace=dict(self._models))
        return model


__all__ = ["SchemaTypes"]
