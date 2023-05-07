""" A high availability GIS client. """
import ast
from _ast import Attribute, BoolOp, Call, Compare, Constant, Name
from concurrent import futures
from datetime import datetime
from hashlib import md5
from inspect import getsource, signature
from itertools import chain, islice
from json import dumps, loads
from time import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Generic, Iterator, List, Optional, Tuple, Type, TypeVar, Union
from uuid import UUID
from requests import post

T = TypeVar("T")


class Layer(Generic[T]):  # pylint: disable=too-many-instance-attributes
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

        self._is_arcgis_gemetry = hasattr(self._shape_property_type, "__module__")\
            and self._shape_property_type.__module__.startswith("arcgis.geometry.")

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

    def query(self, where_clause: Union[str, Callable[[T], bool], None] = None, record_count: Optional[int] = None,
              wkid: Optional[int] = None, max_workers: Optional[int] = None, **kwargs: Any) -> Iterator[T]:
        """ Executes a query.

        Args:
            where_clause (str, optional): Where clause.  Defaults to None.
            record_count (Optional[int], optional): Maximum record count.  Defaults to None.
            wkid (Optional[int], optional): Spatial reference.  Defaults to None.
            max_workers (Optional[int], optional): Max worker count (degree of parallelism). Defaults to None.

        Yields:
            Iterator[T]: Items.
        """
        if not where_clause:
            where_clause = "1=1"
        elif isinstance(where_clause, Callable):
            where_clause = self._to_sql(where_clause)

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
            kwargs["resultRecordCount"] = record_count

        if wkid:
            kwargs["outSR"] = wkid

        for row in islice(self._query(where_clause, fields, record_count, max_workers, **kwargs), record_count):
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

    def count(self, where_clause: Union[str, Callable[[T], bool], None] = None) -> int:
        """ Checks the number of items that match the where clause.

        Args:
            where_clause (str, optional): Where clause.  Defaults to None.

        Returns:
            int: Count.
        """
        if not where_clause:
            where_clause = "1=1"
        elif isinstance(where_clause, Callable):
            where_clause = self._to_sql(where_clause)

        obj = self._call("query", where=where_clause, returnCountOnly=True)
        return obj.count

    def find(self, oid: int, **kwargs: Any) -> Optional[T]:
        """ Finds the item by Object ID.

        Args:
            oid (int): Object ID.

        Returns:
            Optional[T]: Item if found (otherwise None).
        """
        where_clause = self.generate_where_clause(oid)
        items = list(self.query(where_clause, **kwargs))
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

    def delete(self, where_clause: Union[str, Callable[[T], bool]], **kwargs: Any) -> None:
        """ Deletes items based on a where clause.

        Args:
            where_clause (str): Where clause use for deleting.
        """
        if not where_clause:
            raise ValueError("Where clause is required for the delete operation.")

        if isinstance(where_clause, Callable):
            where_clause = self._to_sql(where_clause)

        self._call("deleteFeatures", where=where_clause, **kwargs)

    def generate_where_clause(self, *ids: Union[int, str, UUID], id_field: Optional[str] = None) -> str:
        """ Generates a where clause from a list of Object ID, Global ID or some other identifiers.

        Args:
            id_field (Optional[str], optional): Name of the ID field. Defaults to None.

        Returns:
            str: Where clause.
        """
        if not ids:
            return "(1=0)"

        if isinstance(ids[0], int):
            field_name = id_field if id_field else self._oid_field
            id_set = set(map(str, ids))
        else:
            field_name = id_field if id_field else "globalid"
            id_set = set((f"'{_id}'" for _id in ids))

        return f"({field_name} IN ({','.join(id_set)}))"

    def _to_dict(self, item: T) -> Dict[str, Any]:
        dictionary: Dict[str, Any] = {}
        attributes: Dict[str, Any] = {}

        dictionary["attributes"] = attributes

        for key, value in item.__dict__.items():
            lower_property_name = key.lower()
            field = self._fields[lower_property_name]
            if lower_property_name == self._shape_property_name.lower():
                if self._is_arcgis_gemetry:
                    dictionary["geometry"] = loads(value.JSON)
                else:
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

        date_fields: List[str] = []
        uuid_fields: List[str] = []

        if hasattr(obj, "fields"):
            for f in obj.fields:
                if f.type == "esriFieldTypeDate":
                    date_fields.append(f.name)
                elif f.type == "esriFieldTypeGlobalID" or f.type == "esriFieldTypeGUID":
                    uuid_fields.append(f.name)

        for feature in obj.features:
            if date_fields:
                for key, value in feature.attributes.__dict__.items():
                    if key in date_fields and value:
                        feature.attributes.__dict__[key] = datetime.utcfromtimestamp(value / 1000)
                    elif key in uuid_fields and value:
                        feature.attributes.__dict__[key] = UUID(value)
            if hasattr(feature, "geometry") and feature.geometry and hasattr(obj, "spatialReference"):
                feature.geometry.spatialReference = obj.spatialReference.__dict__

        return (obj.features, obj.exceededTransferLimit if hasattr(obj, "exceededTransferLimit") else False)

    def _get_oids(self, where_clause: str) -> List[int]:
        obj = self._call("query", where=where_clause, returnIdsOnly="true")
        return obj.objectIds

    def _map(self, row: SimpleNamespace) -> SimpleNamespace:
        attributes = {key.lower(): value for key, value in row.attributes.__dict__.items()}

        if not hasattr(row, "geometry"):
            return SimpleNamespace(**attributes)

        if self._shape_property_type is None or self._shape_property_type in self._unknown_shape_types:
            shape = row.geometry
        else:
            if self._is_arcgis_gemetry:
                shape = self._shape_property_type(row.geometry.__dict__)
            else:
                shape = self._shape_property_type()
                shape.__dict__ = row.geometry.__dict__

        return SimpleNamespace(**attributes, **{self._shape_property_name: shape})

    def _query(self, where_clause: str, fields: str, record_count: Optional[int], max_workers: Optional[int],
               **kwargs: Any) -> Iterator[SimpleNamespace]:
        def get_rows(where_clause: str):
            return self._get_rows(where_clause, fields, **kwargs)

        rows, exceeded_transfer_limit = get_rows(where_clause)

        for row in rows:
            yield self._map(row)

        if exceeded_transfer_limit and record_count:
            def get_more_rows(batch: List[int]):
                more_rows, _ = get_rows(self.generate_where_clause(*batch))
                return more_rows

            size = len(rows)
            remaining_oids = list(islice(self._get_oids(where_clause)[size:], record_count - size))
            remaining_batches = [remaining_oids[i: i + size] for i in range(len(remaining_oids))[::size]]

            with futures.ThreadPoolExecutor(max_workers) as executor:
                for rows in executor.map(get_more_rows, remaining_batches):
                    for row in rows:
                        yield self._map(row)

    def _to_sql(self, predicate: Callable[[T], bool]) -> str:
        class LambdaFinder(ast.NodeVisitor):
            def __init__(self, expression: Any) -> None:
                super().__init__()

                self.freevars: Dict[str, Any] = {}

                # Check globals.
                for name in expression.__code__.co_names:
                    if name in expression.__globals__:
                        self.freevars[name] = expression.__globals__[name]

                # Capture closure variables.
                closure = expression.__closure__
                if closure:
                    for name, value in zip(expression.__code__.co_freevars, [x.cell_contents for x in closure]):
                        self.freevars[name] = value

                line = getsource(expression).strip()

                if line.endswith(":"):
                    line = f"{line}\n    pass"

                self.visit(ast.parse(line))

            def visit_Lambda(self, node: ast.Lambda) -> Any:  # pylint: disable-all
                self.expression = node

            @staticmethod
            def find(expression: Any):  # pylint: disable-all
                visitor = LambdaFinder(expression)
                return visitor.expression, visitor.freevars

        class LambdaVisitor(ast.NodeVisitor):
            def __init__(self, expression: ast.expr, freevars: Dict[str, Any], fields: Dict[str, str]) -> None:
                super().__init__()
                self._expressions: List[Union[LambdaVisitor, str]] = []
                self._freevars = freevars
                self._fields = fields
                self.visit(expression)

            def visit_Attribute(self, node: Attribute) -> Any:
                attr = node.attr
                value: Any = node.value
                if value.id in self._freevars:
                    self._expressions.append(self._get_sql_value(getattr(self._freevars[value.id], attr)))
                else:
                    self._expressions.append(self._fields[attr])

            def visit_BoolOp(self, node: BoolOp) -> Any:
                self._expressions.append("(")
                expressions: List[Union[LambdaVisitor, str]] = []
                for value in node.values:
                    expressions.append(LambdaVisitor(value, self._freevars, self._fields))
                    expressions.append(self._convert_op(node.op))
                expressions.pop()
                self._expressions.extend(expressions)
                self._expressions.append(")")

            def visit_Call(self, node: Call) -> Any:
                if not hasattr(node.func, "attr"):
                    self.generic_visit(node)
                    return
                attr = node.func.attr  # type: ignore
                if attr == "startswith":
                    field_name = self._fields[node.func.value.attr]  # type: ignore
                    self._expressions.append(f"{field_name} LIKE '{self._get_value(node.args[0])}%'")
                elif attr == "endswith":
                    field_name = self._fields[node.func.value.attr]  # type: ignore
                    self._expressions.append(f"{field_name} LIKE '%{self._get_value(node.args[0])}'")

            def visit_Compare(self, node: Compare) -> Any:
                op = node.ops[0]
                if isinstance(op, ast.In):
                    field_name = self._fields[node.comparators[0].attr]  # type: ignore
                    self._expressions.append(f"{field_name} LIKE '%{self._get_value(node.left)}%'")
                else:
                    self._expressions.append(LambdaVisitor(node.left, self._freevars, self._fields))
                    self._expressions.append(self._convert_op(node.ops[0]))
                    self._expressions.append(LambdaVisitor(node.comparators[0], self._freevars, self._fields))

            def visit_Constant(self, node: Constant) -> Any:
                self._expressions.append(self._get_sql_value(node.value))

            def visit_Name(self, node: Name) -> Any:
                self._expressions.append(self._get_sql_value(self._freevars[node.id]))

            def _get_sql_value(self, value: Any) -> str:
                if value is None:
                    return "NULL"
                if isinstance(value, str):
                    return f"'{value}'"
                if isinstance(value, datetime):
                    return f"timestamp '{value:%Y-%m-%d %H:%M:%S}'"
                return str(value)

            def _get_value(self, node: Any) -> Any:
                return self._freevars[node.id] if isinstance(node, Name) else node.value

            def _convert_op(self, op: Any) -> str:
                if isinstance(op, ast.And):
                    return "AND"
                if isinstance(op, ast.Or):
                    return "OR"
                if isinstance(op, ast.Is):
                    return "IS"
                if isinstance(op, ast.IsNot):
                    return "IS NOT"
                if isinstance(op, ast.Eq):
                    return "="
                if isinstance(op, ast.NotEq):
                    return "<>"
                if isinstance(op, ast.Gt):
                    return ">"
                if isinstance(op, ast.GtE):
                    return ">="
                if isinstance(op, ast.Lt):
                    return "<"
                if isinstance(op, ast.LtE):
                    return "<="
                return type(op).__name__

            def to_sql(self) -> str:
                text = ""
                for e in self._expressions:
                    text += e.to_sql() if isinstance(e, LambdaVisitor) else f" {e}"
                return text

        # Find the lambda expression and any free variables encapsulated in it.
        expression, freevars = LambdaFinder.find(predicate)

        # Generate a where clause.
        where_clause = LambdaVisitor(expression, freevars, self._fields).to_sql().strip()

        return where_clause


class Point:  # pylint: disable=too-few-public-methods
    """ Point class.
    """
    x: float
    y: float


class MultiPoint:  # pylint: disable=too-few-public-methods
    """ MultiPoint class.
    """
    points: List[List[float]]


class Polyline:  # pylint: disable=too-few-public-methods
    """ Polyline class.
    """
    paths: List[List[List[float]]]


class Polygon:  # pylint: disable=too-few-public-methods
    """ Polygon class.
    """
    rings: List[List[List[float]]]
