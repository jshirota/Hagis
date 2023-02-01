from datetime import datetime
from json import loads
from requests import get
from types import SimpleNamespace
from typing import Any, Dict, Generic, Iterator, List, Tuple, Type, TypeVar
from urllib.parse import urlencode


class AttGeoBase:

    def __init__(self, layer_url: str, oid_field: str, shape_property: str) -> None:

        self._layer_url = layer_url
        self._oid_field = oid_field
        self._shape_property = shape_property

    def _query(self, **kwargs: Any) -> SimpleNamespace:

        response = get(f"{self._layer_url}/query?{urlencode(kwargs)}")

        # Map it to a dynamic object.
        obj = loads(response.text, object_hook=lambda x: SimpleNamespace(**x))

        # If this is an error response, throw an exception.
        if hasattr(obj, "error"):
            raise Exception(obj.error.message)

        return obj

    def _get_rows(self, where_clause: str, fields: str, **kwargs: Any) -> Tuple[List[SimpleNamespace], bool]:

        obj = self._query(where=where_clause, outFields=fields, f="json", **kwargs)

        date_fields = [f.name for f in obj.fields if f.type == "esriFieldTypeDate"]

        if date_fields:
            for f in obj.features:
                for key, value in f.attributes.__dict__.items():
                    if key in date_fields and value:
                        f.attributes.__dict__[key] = datetime.fromtimestamp(value / 1000)

        return (obj.features, obj.exceededTransferLimit if hasattr(obj, "exceededTransferLimit") else False)

    def _get_oids(self, where_clause: str) -> List[int]:

        obj = self._query(where=where_clause, returnIdsOnly="true", f="json")

        return obj.objectIds

    def _map(self, row: SimpleNamespace) -> SimpleNamespace:

        if hasattr(row, "geometry"):
            return SimpleNamespace(**row.attributes.__dict__, **{self._shape_property: row.geometry})
        else:
            return row.attributes

    def _read(self, where_clause: str, fields: str, **kwargs: Any) -> Iterator[SimpleNamespace]:

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


T = TypeVar("T")


class AttGeo(Generic[T], AttGeoBase):
    """ 

    Args:
        Generic (T): Type argument.
        AttGeoBase: Base class.
    """

    def __init__(self, layer_url: str, model: Type[T] = SimpleNamespace, oid_field: str = "objectid", shape_property: str = "shape") -> None:
        """ Creates a new instance of the AttGeo class.

        Args:
            layer_url (str): Layer url (e.g. .../FeatureServer/0).
            model (Type[T], optional): Model to map to.  Defaults to SimpleNamespace.
            oid_field (str, optional): Name of the Object ID field.  Defaults to "objectid".
            shape_property (str, optional): Name of the shape property.  Defaults to "shape".
        """

        super().__init__(layer_url, oid_field, shape_property)

        self._model = model
        self._fields: Dict[str, str] = {}
        self._is_dynamic = model == SimpleNamespace

        if self._is_dynamic:
            return

        for type in reversed(model.__mro__):
            if hasattr(type, "__annotations__"):
                for property_name in type.__annotations__:
                    key = property_name.lower()
                    self._fields[key] = property_name

                    if key == shape_property.lower():
                        self._shape_property = property_name

    def read(self, where_clause: str = "1=1", **kwargs: Any) -> Iterator[T]:
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
            fields = ",".join([f for f in self._fields if f != self._shape_property.lower()])

            if not self._shape_property:
                kwargs["returnGeometry"] = False

        for row in super()._read(where_clause, fields, **kwargs):
            if self._is_dynamic:
                yield row  # type: ignore
            else:
                item = self._model()
                for property_name in self._fields.values():
                    value = getattr(row, property_name)
                    setattr(item, property_name, value)
                yield item
