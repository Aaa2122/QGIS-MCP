from __future__ import annotations

import json


def algorithm_schemas(algorithm):
    properties = {}
    required = []
    parameters = []
    for item in algorithm.parameterDefinitions():
        description = parameter_description(item)
        parameters.append(description)
        properties[item.name()] = description["schema"]
        if not description["optional"] and item.defaultValue() is None:
            required.append(item.name())
    output_properties = {
        item.name(): output_schema(item) for item in algorithm.outputDefinitions()
    }
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        input_schema["required"] = required
    return {
        "parameters": parameters,
        "input_schema": input_schema,
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": output_properties,
            "additionalProperties": True,
        },
    }


def parameter_description(item):
    optional = _is_optional(item)
    schema = parameter_schema(item)
    schema["title"] = item.description()
    default = _json_safe(item.defaultValue())
    if default is not None:
        schema["default"] = default
    help_text = _call(item, "help", None)
    if help_text:
        schema["description"] = help_text
    schema["x-qgis-parameter-type"] = item.type()
    schema["x-qgis-flags"] = int(item.flags())
    metadata = _json_safe(_call(item, "metadata", {}))
    if metadata:
        schema["x-qgis-metadata"] = metadata
    if _call(item, "isDynamic", False):
        schema["x-qgis-dynamic"] = True
        property_definition = _call(item, "dynamicPropertyDefinition", None)
        if property_definition is not None:
            schema["x-qgis-dynamic-property"] = _json_safe(property_definition)
    return {
        "name": item.name(),
        "description": item.description(),
        "type": item.type(),
        "optional": optional,
        "default": default,
        "schema": schema,
    }


def parameter_schema(item):
    kind = str(item.type()).casefold()
    class_name = type(item).__name__.casefold()
    token = "{} {}".format(kind, class_name)

    if "boolean" in token:
        return {"type": "boolean"}
    if any(name in token for name in ("distance", "scale")):
        return _numeric_schema(item, integer=False)
    if "number" in token:
        data_type = _call(item, "dataType", None)
        return _numeric_schema(item, integer=str(data_type).endswith("Integer") or data_type == 0)
    if "enum" in token:
        options = list(_call(item, "options", []) or [])
        base = {"type": "integer", "minimum": 0}
        if options:
            base["maximum"] = len(options) - 1
            base["x-qgis-enum-options"] = options
        return {"type": "array", "items": base, "uniqueItems": True} if _call(
            item, "allowMultiple", False
        ) else base
    if "multiplelayers" in token:
        return {"type": "array", "items": _layer_reference_schema(item)}
    if "field" in token:
        base = {"type": "string", "x-qgis-parent-layer-parameter": _call(item, "parentLayerParameterName", None)}
        return {"type": "array", "items": base, "uniqueItems": True} if _call(
            item, "allowMultiple", False
        ) else base
    if "band" in token:
        base = {"type": "integer", "minimum": 1}
        return {"type": "array", "items": base, "uniqueItems": True} if _call(
            item, "allowMultiple", False
        ) else base
    if "extent" in token:
        return {"oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            {"type": "object", "required": ["xmin", "ymin", "xmax", "ymax"], "properties": {key: {"type": "number"} for key in ("xmin", "ymin", "xmax", "ymax")}, "additionalProperties": False},
        ]}
    if "point" in token and "pointcloud" not in token:
        return {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}]}
    if "geometry" in token:
        return {"oneOf": [{"type": "string"}, {"type": "object"}]}
    if any(name in token for name in ("maplayer", "vectorlayer", "rasterlayer", "meshlayer", "pointcloud", "featuresource", "source")):
        return _layer_reference_schema(item)
    if any(name in token for name in ("sink", "destination")):
        return {"type": "string", "x-qgis-output-destination": True}
    if "datetime" in token:
        return {"type": "string", "format": "date-time"}
    if "date" in token:
        return {"type": "string", "format": "date"}
    if "time" in token:
        return {"type": "string", "format": "time"}
    if "matrix" in token:
        return {"type": "array", "items": {"type": "array"}}
    if any(name in token for name in ("string", "expression", "crs", "file", "folder", "authcfg", "layout", "color")):
        return {"type": "string"}
    return {}


def output_schema(item):
    token = "{} {}".format(item.type(), type(item).__name__).casefold()
    schema = {"title": item.description(), "x-qgis-output-type": item.type()}
    if "number" in token:
        schema["type"] = "number"
    elif "boolean" in token:
        schema["type"] = "boolean"
    elif "multiple" in token:
        schema.update({"type": "array", "items": {}})
    elif any(name in token for name in ("layer", "sink", "destination", "file", "folder", "html")):
        schema["type"] = "string"
    else:
        schema.update({"type": ["string", "number", "boolean", "object", "array", "null"]})
    return schema


def _layer_reference_schema(item):
    return {
        "type": "string",
        "description": "QGIS layer ID, layer name, provider URI, or file path",
        "x-qgis-layer-types": _json_safe(_call(item, "dataTypes", [])),
    }


def _numeric_schema(item, integer):
    schema = {"type": "integer" if integer else "number"}
    minimum = _call(item, "minimum", None)
    maximum = _call(item, "maximum", None)
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _is_optional(item):
    flag = getattr(item, "FlagOptional", None)
    return bool(flag is not None and item.flags() & flag)


def _call(obj, name, default):
    method = getattr(obj, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:
        return default


def _json_safe(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)
