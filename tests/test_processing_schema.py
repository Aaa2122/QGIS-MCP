from __future__ import annotations

from qgis_agent_mcp.processing_schema import algorithm_schemas


class Parameter:
    FlagOptional = 1

    def __init__(self, name, kind, optional=False, default=None, **values):
        self._name = name
        self._kind = kind
        self._flags = self.FlagOptional if optional else 0
        self._default = default
        self._values = values

    def name(self):
        return self._name

    def type(self):
        return self._kind

    def description(self):
        return self._name.title()

    def flags(self):
        return self._flags

    def defaultValue(self):
        return self._default

    def __getattr__(self, name):
        if name not in self._values:
            raise AttributeError(name)
        return lambda: self._values[name]


class Output(Parameter):
    pass


class Algorithm:
    def parameterDefinitions(self):
        return [
            Parameter("INPUT", "source"),
            Parameter("DISTANCE", "distance", default=10.0, minimum=0.0),
            Parameter("MODE", "enum", options=["fast", "exact"]),
            Parameter("FIELDS", "field", optional=True, allowMultiple=True),
            Parameter("EXTENT", "extent", optional=True),
        ]

    def outputDefinitions(self):
        return [Output("OUTPUT", "vectorDestination")]


def test_processing_parameters_have_complete_machine_readable_schemas():
    schemas = algorithm_schemas(Algorithm())
    inputs = schemas["input_schema"]
    assert inputs["required"] == ["INPUT", "MODE"]
    assert inputs["properties"]["INPUT"]["type"] == "string"
    assert inputs["properties"]["DISTANCE"]["minimum"] == 0.0
    assert inputs["properties"]["MODE"]["x-qgis-enum-options"] == ["fast", "exact"]
    assert inputs["properties"]["FIELDS"]["type"] == "array"
    assert len(inputs["properties"]["EXTENT"]["oneOf"]) == 3
    assert schemas["output_schema"]["properties"]["OUTPUT"]["type"] == "string"
