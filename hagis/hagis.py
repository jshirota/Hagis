""" A high availability GIS client. """
from datetime import datetime
from hashlib import md5
from inspect import signature
from itertools import chain, islice
from json import dumps, loads
from time import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Generic, Iterable, List, Optional, Tuple, Type, TypeVar, Union
from requests import post

T = TypeVar("T")

class Layer(Generic[T]): # pylint: disable=too-many-instance-attributes
    """ Layer class.

    Args:
        Generic (T): Type argument.
    """
    def __init__(self, layer_url: str, model: Type[T] = SimpleNamespace,
                 oid_field: str = "objectid", shape_property_name: str = "shape", **mapping: str) -> None:
        """ Creates a new instance of the Layer class.

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
        self._generate_token: Callable[[], str] = lambda: ""

        self._has_parameterless_constructor = len(set(chain(
            signature(model.__init__).parameters.keys(),
            signature(model.__new__).parameters.keys()))) == 3

        self._is_dynamic = issubclass(model, SimpleNamespace)

        if self._is_dynamic:
            return

        # List of custom mapping properties that have been handled.
        mapped: List[str] = []

        for model_type in reversed(model.mro()):
            if hasattr(model_type, "__annotations__"):
                for property_name, property_type in model_type.__annotations__.items():
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
        for property_name, field in mapping.items():
            if property_name not in mapped:
                self._fields[property_name.lower()] = field

    _token_cache: Dict[Tuple[str, str], Tuple[str, int]] = {}

    def set_token_generator(self, username: str, password: str, referer: str = "",
                            token_url: str = "https://www.arcgis.com/sharing/generateToken", **kwargs: Any) -> None:
        """ Sets the token generation parameters.

        Args:
            username (str): User name.
            password (str): Password.
            referer (str, optional): Referer.  Defaults to "".
            token_url (str, optional): Endpoint.  Defaults to "https://www.arcgis.com/sharing/generateToken".
        """
        kwargs["username"] = username
        kwargs["password"] = password
        kwargs["referer"] = referer
        kwargs["client"] = "referer" if referer else "ip" if "ip" in kwargs else "requestip"

        key = token_url, md5(dumps(kwargs).encode("utf-8")).hexdigest()

        def generate_token() -> str:
            if key not in Layer._token_cache:
                Layer._token_cache[key] = "", 0

            # Get the cached token and its expiry.
            token, expiration_seconds = Layer._token_cache[key]

            # Renew if less than a minute left.
            if expiration_seconds - time() < 60:
                obj = self._post(token_url, **kwargs)
                token, expiration_seconds = obj.token, obj.expires / 1000
                Layer._token_cache[key] = token, expiration_seconds

            return token

        self._generate_token = generate_token

    def set_token(self, token: str) -> None:
        """ Sets the static token.

        Args:
            token (str): Token.
        """
        self._generate_token = lambda: token

    def query(self, where_clause: Optional[str] = None, record_count: Optional[int] = None, wkid: Optional[int] = None,
              **kwargs: Any) -> Iterable[T]:
        """ Executes a query.

        Args:
            where_clause (str, optional): Where clause.  Defaults to None.
            record_count (Optional[int], optional): Maximum record count.  Defaults to None.
            wkid (Optional[int], optional): Spatial reference.  Defaults to None.

        Returns:
            Iterable[T]: Items.
        """
        if not where_clause:
            where_clause = "1=1"

        if record_count == 0:
            return

        if self._is_dynamic:
            # If dynamic, request all fields.
            fields = "*"
        else:
            # Otherwise, request only what is used by the model.
            fields = ",".join([f for (_, f) in self._fields.items() if f != self._shape_property_name.lower()])
            if not self._shape_property_name:
                kwargs["returnGeometry"] = False

        if record_count:
            keep_querying = True
            kwargs["resultRecordCount"] = record_count
        else:
            keep_querying = False

        if wkid:
            kwargs["outSR"] = wkid

        for row in islice(self._query(where_clause, fields, keep_querying, **kwargs), record_count):
            if self._is_dynamic:
                yield row  # type: ignore
            else:
                dictionary = {property: getattr(row, field) for (property, field) in self._fields.items()}
                if self._has_parameterless_constructor:
                    item = self._model()
                    for property_name, property_value in dictionary.items():
                        setattr(item, property_name, property_value)
                else:
                    # Support for data classes and named tuples.
                    item = self._model(*dictionary.values())

                yield item

    def count(self, where_clause: Optional[str] = None) -> int:
        """ Checks the number of items that match the where clause.

        Args:
            where_clause (str, optional): Where clause.  Defaults to None.

        Returns:
            int: Count.
        """
        if not where_clause:
            where_clause = "1=1"

        obj = self._call("query", where=where_clause, returnCountOnly=True)
        return obj.count

    def find(self, oid: int, **kwargs: Any) -> Optional[T]:
        """ Finds the item by Object ID.

        Args:
            oid (int): Object ID.

        Returns:
            Optional[T]: Item if found (otherwise None).
        """
        items = list(self.query(f"{self._oid_field}={oid}", **kwargs))
        return items[0] if items else None

    def apply_edits(self,
                    adds: Optional[List[T]] = None,
                    updates: Optional[List[T]] = None,
                    deletes: Union[List[int], List[str], None] = None, **kwargs: Any) -> SimpleNamespace:
        """ Applies multiple edits atomically.

        Args:
            adds (Optional[List[T]], optional): Items to insert.  Defaults to None.
            updates (Optional[List[T]], optional): Items to update.  Defaults to None.
            deletes (Union[List[int], List[str], None], optional): Object IDs of items to delete.  Defaults to None.

        Returns:
            SimpleNamespace: Edit result object.
        """
        adds_json = "" if adds is None else dumps([self._to_dict(x) for x in adds])
        updates_json = "" if updates is None else dumps([self._to_dict(x) for x in updates])
        deletes_json = "" if deletes is None else dumps([x for x in deletes])
        return self._call("applyEdits", adds=adds_json, updates=updates_json, deletes=deletes_json, **kwargs)

    def insert(self, items: List[T], **kwargs: Any) -> List[int]:
        """ Inserts new items on the remote server.

        Args:
            items (List[T]): Items to insert.

        Returns:
            List[int]: Object IDs of the newly created items.
        """
        result = self.apply_edits(adds=items, **kwargs)
        return [x.objectId for x in result.addResults]

    def update(self, items: List[T], **kwargs: Any) -> None:
        """ Updates existing items on the remote server.

        Args:
            items (List[T]): Items to update.
        """
        self.apply_edits(updates=items, **kwargs)

    def delete(self, where_clause: str, **kwargs: Any) -> None:
        """ Deletes items based on a where clause.

        Args:
            where_clause (str): Where clause use for deleting.
        """
        self._call("deleteFeatures", where=where_clause, **kwargs)

    def _to_dict(self, item: T) -> Dict[str, Any]:
        dictionary: Dict[str, Any] = {}
        attributes: Dict[str, Any] = {}

        dictionary["attributes"] = attributes

        for key, value in item.__dict__.items():
            lower_property_name = key.lower()
            field = self._fields[lower_property_name]
            if lower_property_name == self._shape_property_name.lower():
                dictionary["geometry"] = value.__dict__
            elif isinstance(value, datetime):
                attributes[field] = int((value - datetime.utcfromtimestamp(0)).total_seconds() * 1000)
            else:
                attributes[field] = value

        return dictionary

    def _post(self, url: str, **kwargs: Any) -> SimpleNamespace:
        kwargs["f"] = "json"
        response = post(url, data=kwargs, timeout=10)
        obj = loads(response.text, object_hook=lambda x: SimpleNamespace(**x))

        if hasattr(obj, "error"):
            raise RuntimeError(obj.error.message)

        return obj

    def _call(self, method: str, **kwargs: Any) -> SimpleNamespace:
        kwargs["token"] = self._generate_token()
        return self._post(f"{self._layer_url}/{method}", **kwargs)

    def _get_rows(self, where_clause: str, fields: str, **kwargs: Any) -> Tuple[List[SimpleNamespace], bool]:
        obj = self._call("query", where=where_clause, outFields=fields, **kwargs)
        date_fields = [f.name for f in obj.fields if f.type == "esriFieldTypeDate"] if hasattr(obj, "fields") else []

        if date_fields:
            for feature in obj.features:
                for key, value in feature.attributes.__dict__.items():
                    if key in date_fields and value:
                        feature.attributes.__dict__[key] = datetime.fromtimestamp(value / 1000)

        return (obj.features, obj.exceededTransferLimit if hasattr(obj, "exceededTransferLimit") else False)

    def _get_oids(self, where_clause: str) -> List[int]:
        obj = self._call("query", where=where_clause, returnIdsOnly="true")
        return obj.objectIds

    def _map(self, row: SimpleNamespace) -> SimpleNamespace:
        if not hasattr(row, "geometry"):
            return row.attributes

        if self._shape_property_type is None or self._shape_property_type in self._unknown_shape_types:
            shape = row.geometry
        else:
            shape = self._shape_property_type()
            shape.__dict__ = row.geometry.__dict__

        return SimpleNamespace(**row.attributes.__dict__, **{self._shape_property_name: shape})

    def _query(self, where_clause: str, fields: str, keep_querying: bool, **kwargs: Any) -> Iterable[SimpleNamespace]:
        def get_rows(where_clause: str):
            return self._get_rows(where_clause, fields, **kwargs)

        rows, exceeded_transfer_limit = get_rows(where_clause)

        for row in rows:
            yield self._map(row)

        if exceeded_transfer_limit and keep_querying:
            size = len(rows)
            oids = self._get_oids(where_clause)
            for number in range(size, len(oids), size):
                more_where_clause = f"{self._oid_field} IN ({','.join(map(str, oids[number:number+size]))})"
                more_rows, _ = get_rows(more_where_clause)
                for row in more_rows:
                    yield self._map(row)


class Point: # pylint: disable=too-few-public-methods
    """ Point class.
    """
    x: float
    y: float

class Multipoint: # pylint: disable=too-few-public-methods
    """ Multipoint class.
    """
    points: List[List[float]]

class Polyline: # pylint: disable=too-few-public-methods
    """ Polyline class.
    """
    paths: List[List[List[float]]]

class Polygon: # pylint: disable=too-few-public-methods
    """ Polygon class.
    """
    rings: List[List[List[float]]]
