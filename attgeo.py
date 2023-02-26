from datetime import datetime
from json import loads
from requests import post
from types import SimpleNamespace
from typing import Any, Dict, Generic, Iterator, List, Tuple, Type, TypeVar


T = TypeVar("T")


class Mapper(Generic[T]):
    """ 

    Args:
        Generic (T): Type argument.
        AttGeoBase: Base class.
    """

    def __init__(self, layer_url: str, model: Type[T] = SimpleNamespace, oid_field: str = "objectid", shape_property_name: str = "shape", mapping: Dict[str, str] = {}) -> None:
        """ Creates a new instance of the AttGeo class.

        Args:
            layer_url (str): Layer url (e.g. .../FeatureServer/0).
            model (Type[T], optional): Model to map to.  Defaults to SimpleNamespace.
            oid_field (str, optional): Name of the Object ID field.  Defaults to "objectid".
            shape_property_name (str, optional): Name of the shape property.  Defaults to "shape".
        """

        self._layer_url = layer_url
        self._model = model
        self._oid_field = oid_field
        self._shape_property_name = shape_property_name
        self._shape_property_type = None
        self._unknown_shape_types = [Any, object, SimpleNamespace]
        self._fields: Dict[str, str] = {}
        self._is_dynamic = model == SimpleNamespace

        if self._is_dynamic:
            return
        
        # List of custom mapping properties that have been handled.
        mapped: List[str] = []

        for type in reversed(model.__mro__):
            if hasattr(type, "__annotations__"):
                for property_name, property_type in type.__annotations__.items():

                    key = property_name.lower()

                    if property_name in mapping:
                        self._fields[key] = mapping[property_name]
                        mapped.append(property_name)
                    else:
                        self._fields[key] = property_name

                    if key == shape_property_name.lower():
                        self._shape_property_name = property_name
                        self._shape_property_type = property_type

        # Add custom properties that have not been handled as dynamically handled propeties.
        for (property, field) in mapping.items():
            if property not in mapped:
                self._fields[property.lower()] = field

    def query(self, where_clause: str = "1=1", **kwargs: Any) -> Iterator[T]:
        """ Queries the feature layer and yields the results as typed objects.

        Args:
            where_clause (str, optional): Where clause.  Defaults to "1=1".

        Yields:
            Iterator[T]: Lazily generated objects of specified type.
        """

        if self._is_dynamic:
            # If dynamic, request all fields.
            fields = "*"
        else:
            # Otherwise, request only what is used by the model.
            fields = ",".join([f for (_, f) in self._fields.items() if f != self._shape_property_name.lower()])

            if not self._shape_property_name:
                kwargs["returnGeometry"] = False

        for row in self._query(where_clause, fields, **kwargs):
            if self._is_dynamic:
                yield row  # type: ignore
            else:                
                dictionary = {property: getattr(row, field) for (property, field) in self._fields.items()}
                if hasattr(self._model, "__dataclass_fields__"):
                    # Support for dataclasses.
                    item = self._model(*dictionary.values())
                else:
                    # Normal classes require the parameterless constructor.
                    item = self._model()
                    for property_name, property_value in dictionary.items():
                        setattr(item, property_name, property_value)
                yield item

    def apply_edits(self, items: List[T]):
        print([self._to_dict(x) for x in items])

    def _to_dict(self, item: T) -> Dict[str, Any]:

        dictionary: Dict[str, Any] = {}
        attributes: Dict[str, Any] = {}
        dictionary["attributes"] = attributes

        for key, value in item.__dict__.items():

            property = key.lower()
            field = self._fields[property]

            if property == self._shape_property_name:
                dictionary["geometry"] = value.__dict__
            elif isinstance(value, datetime):
                attributes[field] = int((value - datetime.utcfromtimestamp(0)).total_seconds() * 1000)
            else:
                attributes[field] = value

        return dictionary

    def _post(self, **kwargs: Any) -> SimpleNamespace:

        response = post(f"{self._layer_url}/query", kwargs)

        # Map it to a dynamic object.
        obj = loads(response.text, object_hook=lambda x: SimpleNamespace(**x))

        # If this is an error response, throw an exception.
        if hasattr(obj, "error"):
            raise Exception(obj.error.message)

        return obj

    def _get_rows(self, where_clause: str, fields: str, **kwargs: Any) -> Tuple[List[SimpleNamespace], bool]:

        obj = self._post(where=where_clause, outFields=fields, f="json", **kwargs)

        date_fields = [f.name for f in obj.fields if f.type == "esriFieldTypeDate"]

        if date_fields:
            for f in obj.features:
                for key, value in f.attributes.__dict__.items():
                    if key in date_fields and value:
                        f.attributes.__dict__[key] = datetime.fromtimestamp(value / 1000)

        return (obj.features, obj.exceededTransferLimit if hasattr(obj, "exceededTransferLimit") else False)

    def _get_oids(self, where_clause: str) -> List[int]:

        obj = self._post(where=where_clause, returnIdsOnly="true", f="json")

        return obj.objectIds

    def _map(self, row: SimpleNamespace) -> SimpleNamespace:

        if hasattr(row, "geometry"):
            if self._shape_property_type is None or self._shape_property_type in self._unknown_shape_types:
                shape = row.geometry
            else:
                shape = self._shape_property_type()
                shape.__dict__ = row.geometry.__dict__
            return SimpleNamespace(**row.attributes.__dict__, **{self._shape_property_name: shape})
        else:
            return row.attributes

    def _query(self, where_clause: str, fields: str, **kwargs: Any) -> Iterator[SimpleNamespace]:

        def get_rows(where_clause: str):
            return self._get_rows(where_clause, fields, **kwargs)

        rows, exceededTransferLimit = get_rows(where_clause)

        for row in rows:
            yield self._map(row)

        if exceededTransferLimit:
            size = len(rows)
            oids = self._get_oids(where_clause)
            for n in range(size, len(oids), size):
                more_where_clause = f"{self._oid_field} IN ({','.join(map(str, oids[n:n+size]))})"
                more_rows, _ = get_rows(more_where_clause)
                for row in more_rows:
                    yield self._map(row)


class Point:
    x: float
    y: float

class Multipoint:
    points: List[List[float]]

class Polyline:
    paths: List[List[List[float]]]

class Polygon:
    rings: List[List[List[float]]]
