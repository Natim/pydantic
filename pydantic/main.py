# remove this once issues are resolved
# flake8: noqa
import warnings
from abc import ABCMeta
from copy import deepcopy
from enum import Enum
from pathlib import Path
from types import FunctionType, prepare_class, resolve_bases
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Callable,
    ClassVar,
    Dict,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    no_type_check,
    overload,
)

from typing_extensions import dataclass_transform

from ._internal.new_model import NewModel
from ._internal.typing_extra import AnyCallable, is_namedtuple, update_model_forward_refs
from ._internal.valdation_functions import ValidationFunctions
from .config import BaseConfig, Extra, inherit_config
from .error_wrappers import ErrorWrapper, ValidationError
from .errors import ConfigError, DictError
from .fields import MAPPING_LIKE_SHAPES, Field, FieldInfo, ModelPrivateAttr, Undefined
from .parse import Protocol, load_file, load_str_bytes
from .schema import default_ref_template, model_schema
from .types import StrBytes
from .utils import DUNDER_ATTRIBUTES, ROOT_KEY, GetterDict, Representation, ValueItems, is_valid_field, sequence_like

if TYPE_CHECKING:
    from inspect import Signature

    from ._internal.typing_extra import (
        AbstractSetIntStr,
        AnyClassMethod,
        CallableGenerator,
        DictAny,
        DictStrAny,
        MappingIntStrAny,
        ReprArgs,
        SetStr,
        TupleGenerator,
    )

    Model = TypeVar('Model', bound='BaseModel')

__all__ = (
    'BaseModel',
    'create_model',
)

_T = TypeVar('_T')


# If a field is of type `Callable`, its default value should be a function and cannot to ignored.
ANNOTATED_FIELD_UNTOUCHED_TYPES: Tuple[Any, ...] = (property, type, classmethod, staticmethod)
# When creating a `BaseModel` instance, we bypass all the methods, properties... added to the model
UNTOUCHED_TYPES: Tuple[Any, ...] = (FunctionType,) + ANNOTATED_FIELD_UNTOUCHED_TYPES
# Note `ModelMetaclass` refers to `BaseModel`, but is also used to *create* `BaseModel`, so we need to add this extra
# (somewhat hacky) boolean to keep track of whether we've created the `BaseModel` class yet, and therefore whether it's
# safe to refer to it. If it *hasn't* been created, we assume that the `__new__` call we're in the middle of is for
# the `BaseModel` class, since that's defined immediately after the metaclass.
_base_class_defined = False


@dataclass_transform(kw_only_default=True, field_specifiers=(Field, FieldInfo))
class ModelMetaclass(ABCMeta):
    def __new__(  # noqa C901
        mcs, name: str, bases: Tuple[Type[Any], ...], namespace: Dict[str, Any], **kwargs: Any
    ) -> type:
        if _base_class_defined:
            model_creator = NewModel(mcs, name, bases, namespace, kwargs)
            model_creator.inherit()
            model_creator.prepare_config()
            model_creator.inspect_annotations()
            model_creator.inspect_namespace()
            new_namespace, inner_schema = model_creator.new_namespace()
            cls = super().__new__(mcs, name, bases, new_namespace, **kwargs)
            model_creator.complete_class(cls, inner_schema)
            return cls
        else:
            # this is the BaseModel class itself being created, no logic required
            return super().__new__(mcs, name, bases, namespace, **kwargs)

    def __instancecheck__(self, instance: Any) -> bool:
        """
        Avoid calling ABC _abc_subclasscheck unless we're pretty sure.

        See #3829 and python/cpython#92810
        """
        return hasattr(instance, '__fields__') and super().__instancecheck__(instance)


object_setattr = object.__setattr__


class BaseModel(Representation, metaclass=ModelMetaclass):
    if TYPE_CHECKING:
        # populated by the metaclass, defined here to help IDEs only
        __validator_functions__: ValidationFunctions
        __fields__: ClassVar[Dict[str, FieldInfo]] = {}
        __validators__: ClassVar[Dict[str, AnyCallable]] = {}
        __config__: ClassVar[Type[BaseConfig]] = BaseConfig
        __json_encoder__: ClassVar[Callable[[Any], Any]] = lambda x: x
        __schema_cache__: ClassVar['DictAny'] = {}
        __signature__: ClassVar['Signature']
        __private_attributes__: ClassVar[Dict[str, ModelPrivateAttr]]
        __class_vars__: ClassVar[SetStr]
        __fields_set__: ClassVar[SetStr] = set()

    Config = BaseConfig
    __slots__ = '__dict__', '__fields_set__'
    __doc__ = ''  # Null out the Representation docstring

    def __init__(__pydantic_self__, **data: Any) -> None:
        """
        Create a new model by parsing and validating input data from keyword arguments.

        Raises ValidationError if the input data cannot be parsed to form a valid model.

        Uses something other than `self` the first arg to allow "self" as a field name
        """
        values, fields_set = __pydantic_self__.__validator__.validate_python(data)
        object_setattr(__pydantic_self__, '__dict__', values)
        object_setattr(__pydantic_self__, '__fields_set__', fields_set)
        __pydantic_self__._init_private_attributes()

    @no_type_check
    def __setattr__(self, name, value):  # noqa: C901 (ignore complexity)
        if name in self.__private_attributes__ or name in DUNDER_ATTRIBUTES:
            return object_setattr(self, name, value)

        if self.__config__.extra is not Extra.allow and name not in self.__fields__:
            raise ValueError(f'"{self.__class__.__name__}" object has no field "{name}"')
        elif not self.__config__.allow_mutation or self.__config__.frozen:
            raise TypeError(f'"{self.__class__.__name__}" is immutable and does not support item assignment')
        elif name in self.__fields__ and self.__fields__[name].final:
            raise TypeError(
                f'"{self.__class__.__name__}" object "{name}" field is final and does not support reassignment'
            )
        elif self.__config__.validate_assignment:
            new_values = {**self.__dict__, name: value}

            # for validator in self.__pre_root_validators__:
            #     try:
            #         new_values = validator(self.__class__, new_values)
            #     except (ValueError, TypeError, AssertionError) as exc:
            #         raise ValidationError([ErrorWrapper(exc, loc=ROOT_KEY)], self.__class__)

            known_field = self.__fields__.get(name, None)
            if known_field:
                # We want to
                # - make sure validators are called without the current value for this field inside `values`
                # - keep other values (e.g. submodels) untouched (using `BaseModel.dict()` will change them into dicts)
                # - keep the order of the fields
                if not known_field.field_info.allow_mutation:
                    raise TypeError(f'"{known_field.name}" has allow_mutation set to False and cannot be assigned')
                dict_without_original_value = {k: v for k, v in self.__dict__.items() if k != name}
                value, error_ = known_field.validate(value, dict_without_original_value, loc=name, cls=self.__class__)
                if error_:
                    raise ValidationError([error_], self.__class__)
                else:
                    new_values[name] = value

            errors = []
            # for skip_on_failure, validator in self.__post_root_validators__:
            #     if skip_on_failure and errors:
            #         continue
            #     try:
            #         new_values = validator(self.__class__, new_values)
            #     except (ValueError, TypeError, AssertionError) as exc:
            #         errors.append(ErrorWrapper(exc, loc=ROOT_KEY))
            if errors:
                raise ValidationError(errors, self.__class__)

            # update the whole __dict__ as other values than just `value`
            # may be changed (e.g. with `root_validator`)
            object_setattr(self, '__dict__', new_values)
        else:
            self.__dict__[name] = value

        self.__fields_set__.add(name)

    def __getstate__(self) -> 'DictAny':
        private_attrs = ((k, getattr(self, k, Undefined)) for k in self.__private_attributes__)
        return {
            '__dict__': self.__dict__,
            '__fields_set__': self.__fields_set__,
            '__private_attribute_values__': {k: v for k, v in private_attrs if v is not Undefined},
        }

    def __setstate__(self, state: 'DictAny') -> None:
        object_setattr(self, '__dict__', state['__dict__'])
        object_setattr(self, '__fields_set__', state['__fields_set__'])
        for name, value in state.get('__private_attribute_values__', {}).items():
            object_setattr(self, name, value)

    def _init_private_attributes(self) -> None:
        for name, private_attr in self.__private_attributes__.items():
            default = private_attr.get_default()
            if default is not Undefined:
                object_setattr(self, name, default)

    def dict(
        self,
        *,
        include: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        exclude: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        by_alias: bool = False,
        skip_defaults: Optional[bool] = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
    ) -> 'DictStrAny':
        """
        Generate a dictionary representation of the model, optionally specifying which fields to include or exclude.

        """
        if skip_defaults is not None:
            warnings.warn(
                f'{self.__class__.__name__}.dict(): "skip_defaults" is deprecated and replaced by "exclude_unset"',
                DeprecationWarning,
            )
            exclude_unset = skip_defaults

        return dict(
            self._iter(
                to_dict=True,
                by_alias=by_alias,
                include=include,
                exclude=exclude,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
            )
        )

    def json(
        self,
        *,
        include: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        exclude: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        by_alias: bool = False,
        skip_defaults: Optional[bool] = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        encoder: Optional[Callable[[Any], Any]] = None,
        models_as_dict: bool = True,
        **dumps_kwargs: Any,
    ) -> str:
        """
        Generate a JSON representation of the model, `include` and `exclude` arguments as per `dict()`.

        `encoder` is an optional function to supply as `default` to json.dumps(), other arguments as per `json.dumps()`.
        """
        if skip_defaults is not None:
            warnings.warn(
                f'{self.__class__.__name__}.json(): "skip_defaults" is deprecated and replaced by "exclude_unset"',
                DeprecationWarning,
            )
            exclude_unset = skip_defaults
        encoder = cast(Callable[[Any], Any], encoder or self.__json_encoder__)

        # We don't directly call `self.dict()`, which does exactly this with `to_dict=True`
        # because we want to be able to keep raw `BaseModel` instances and not as `dict`.
        # This allows users to write custom JSON encoders for given `BaseModel` classes.
        data = dict(
            self._iter(
                to_dict=models_as_dict,
                by_alias=by_alias,
                include=include,
                exclude=exclude,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
            )
        )
        return self.__config__.json_dumps(data, default=encoder, **dumps_kwargs)

    @classmethod
    def _enforce_dict_if_root(cls, obj: Any) -> Any:
        if (
            not (isinstance(obj, dict) and obj.keys() == {ROOT_KEY})
            or cls.__fields__[ROOT_KEY].shape in MAPPING_LIKE_SHAPES
        ):
            return {ROOT_KEY: obj}
        else:
            return obj

    @classmethod
    def parse_obj(cls: Type['Model'], obj: Any) -> 'Model':
        obj = cls._enforce_dict_if_root(obj)
        if not isinstance(obj, dict):
            try:
                obj = dict(obj)
            except (TypeError, ValueError) as e:
                exc = TypeError(f'{cls.__name__} expected dict not {obj.__class__.__name__}')
                raise ValidationError([ErrorWrapper(exc, loc=ROOT_KEY)], cls) from e
        return cls(**obj)

    @classmethod
    def parse_raw(
        cls: Type['Model'],
        b: StrBytes,
        *,
        content_type: str = None,
        encoding: str = 'utf8',
        proto: Protocol = None,
        allow_pickle: bool = False,
    ) -> 'Model':
        try:
            obj = load_str_bytes(
                b,
                proto=proto,
                content_type=content_type,
                encoding=encoding,
                allow_pickle=allow_pickle,
                json_loads=cls.__config__.json_loads,
            )
        except (ValueError, TypeError, UnicodeDecodeError) as e:
            raise ValidationError([ErrorWrapper(e, loc=ROOT_KEY)], cls)
        return cls.parse_obj(obj)

    @classmethod
    def parse_file(
        cls: Type['Model'],
        path: Union[str, Path],
        *,
        content_type: str = None,
        encoding: str = 'utf8',
        proto: Protocol = None,
        allow_pickle: bool = False,
    ) -> 'Model':
        obj = load_file(
            path,
            proto=proto,
            content_type=content_type,
            encoding=encoding,
            allow_pickle=allow_pickle,
            json_loads=cls.__config__.json_loads,
        )
        return cls.parse_obj(obj)

    # @classmethod
    # def from_orm(cls: Type['Model'], obj: Any) -> 'Model':
    #     if not cls.__config__.orm_mode:
    #         raise ConfigError('You must have the config attribute orm_mode=True to use from_orm')
    #     obj = cls._decompose_class(obj)
    #     m = cls.__new__(cls)
    #     values, fields_set, validation_error = validate_model(cls, obj)
    #     if validation_error:
    #         raise validation_error
    #     object_setattr(m, '__dict__', values)
    #     object_setattr(m, '__fields_set__', fields_set)
    #     m._init_private_attributes()
    #     return m

    @classmethod
    def construct(cls: Type['Model'], _fields_set: Optional['SetStr'] = None, **values: Any) -> 'Model':
        """
        Creates a new model setting __dict__ and __fields_set__ from trusted or pre-validated data.
        Default values are respected, but no other validation is performed.
        Behaves as if `Config.extra = 'allow'` was set since it adds all passed values
        """
        m = cls.__new__(cls)
        fields_values: Dict[str, Any] = {}
        for name, field in cls.__fields__.items():
            if field.alt_alias and field.alias in values:
                fields_values[name] = values[field.alias]
            elif name in values:
                fields_values[name] = values[name]
            elif not field.required:
                fields_values[name] = field.get_default()
        fields_values.update(values)
        object_setattr(m, '__dict__', fields_values)
        if _fields_set is None:
            _fields_set = set(values.keys())
        object_setattr(m, '__fields_set__', _fields_set)
        m._init_private_attributes()
        return m

    def _copy_and_set_values(self: 'Model', values: 'DictStrAny', fields_set: 'SetStr', *, deep: bool) -> 'Model':
        if deep:
            # chances of having empty dict here are quite low for using smart_deepcopy
            values = deepcopy(values)

        cls = self.__class__
        m = cls.__new__(cls)
        object_setattr(m, '__dict__', values)
        object_setattr(m, '__fields_set__', fields_set)
        for name in self.__private_attributes__:
            value = getattr(self, name, Undefined)
            if value is not Undefined:
                if deep:
                    value = deepcopy(value)
                object_setattr(m, name, value)

        return m

    def copy(
        self: 'Model',
        *,
        include: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        exclude: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        update: Optional['DictStrAny'] = None,
        deep: bool = False,
    ) -> 'Model':
        """
        Duplicate a model, optionally choose which fields to include, exclude and change.

        :param include: fields to include in new model
        :param exclude: fields to exclude from new model, as with values this takes precedence over include
        :param update: values to change/add in the new model. Note: the data is not validated before creating
            the new model: you should trust this data
        :param deep: set to `True` to make a deep copy of the model
        :return: new model instance
        """

        values = dict(
            self._iter(to_dict=False, by_alias=False, include=include, exclude=exclude, exclude_unset=False),
            **(update or {}),
        )

        # new `__fields_set__` can have unset optional fields with a set value in `update` kwarg
        if update:
            fields_set = self.__fields_set__ | update.keys()
        else:
            fields_set = set(self.__fields_set__)

        return self._copy_and_set_values(values, fields_set, deep=deep)

    @classmethod
    def schema(cls, by_alias: bool = True, ref_template: str = default_ref_template) -> 'DictStrAny':
        cached = cls.__schema_cache__.get((by_alias, ref_template))
        if cached is not None:
            return cached
        s = model_schema(cls, by_alias=by_alias, ref_template=ref_template)
        cls.__schema_cache__[(by_alias, ref_template)] = s
        return s

    @classmethod
    def schema_json(
        cls, *, by_alias: bool = True, ref_template: str = default_ref_template, **dumps_kwargs: Any
    ) -> str:
        from .json import pydantic_encoder

        return cls.__config__.json_dumps(
            cls.schema(by_alias=by_alias, ref_template=ref_template), default=pydantic_encoder, **dumps_kwargs
        )

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield cls.validate

    @classmethod
    def validate(cls: Type['Model'], value: Any) -> 'Model':
        if isinstance(value, cls):
            copy_on_model_validation = cls.__config__.copy_on_model_validation
            # whether to deep or shallow copy the model on validation, None means do not copy
            deep_copy: Optional[bool] = None
            if copy_on_model_validation not in {'deep', 'shallow', 'none'}:
                # Warn about deprecated behavior
                warnings.warn(
                    "`copy_on_model_validation` should be a string: 'deep', 'shallow' or 'none'", DeprecationWarning
                )
                if copy_on_model_validation:
                    deep_copy = False

            if copy_on_model_validation == 'shallow':
                # shallow copy
                deep_copy = False
            elif copy_on_model_validation == 'deep':
                # deep copy
                deep_copy = True

            if deep_copy is None:
                return value
            else:
                return value._copy_and_set_values(value.__dict__, value.__fields_set__, deep=deep_copy)

        value = cls._enforce_dict_if_root(value)

        if isinstance(value, dict):
            return cls(**value)
        elif cls.__config__.orm_mode:
            return cls.from_orm(value)
        else:
            try:
                value_as_dict = dict(value)
            except (TypeError, ValueError) as e:
                raise DictError() from e
            return cls(**value_as_dict)

    @classmethod
    def _decompose_class(cls: Type['Model'], obj: Any) -> GetterDict:
        if isinstance(obj, GetterDict):
            return obj
        return cls.__config__.getter_dict(obj)

    @classmethod
    @no_type_check
    def _get_value(
        cls,
        v: Any,
        to_dict: bool,
        by_alias: bool,
        include: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']],
        exclude: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']],
        exclude_unset: bool,
        exclude_defaults: bool,
        exclude_none: bool,
    ) -> Any:

        if isinstance(v, BaseModel):
            if to_dict:
                v_dict = v.dict(
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    include=include,
                    exclude=exclude,
                    exclude_none=exclude_none,
                )
                if ROOT_KEY in v_dict:
                    return v_dict[ROOT_KEY]
                return v_dict
            else:
                return v.copy(include=include, exclude=exclude)

        value_exclude = ValueItems(v, exclude) if exclude else None
        value_include = ValueItems(v, include) if include else None

        if isinstance(v, dict):
            return {
                k_: cls._get_value(
                    v_,
                    to_dict=to_dict,
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    include=value_include and value_include.for_element(k_),
                    exclude=value_exclude and value_exclude.for_element(k_),
                    exclude_none=exclude_none,
                )
                for k_, v_ in v.items()
                if (not value_exclude or not value_exclude.is_excluded(k_))
                and (not value_include or value_include.is_included(k_))
            }

        elif sequence_like(v):
            seq_args = (
                cls._get_value(
                    v_,
                    to_dict=to_dict,
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    include=value_include and value_include.for_element(i),
                    exclude=value_exclude and value_exclude.for_element(i),
                    exclude_none=exclude_none,
                )
                for i, v_ in enumerate(v)
                if (not value_exclude or not value_exclude.is_excluded(i))
                and (not value_include or value_include.is_included(i))
            )

            return v.__class__(*seq_args) if is_namedtuple(v.__class__) else v.__class__(seq_args)

        elif isinstance(v, Enum) and getattr(cls.Config, 'use_enum_values', False):
            return v.value

        else:
            return v

    @classmethod
    def __try_update_forward_refs__(cls, **localns: Any) -> None:
        """
        Same as update_forward_refs but will not raise exception
        when forward references are not defined.
        """
        update_model_forward_refs(cls, cls.__fields__.values(), cls.__config__.json_encoders, localns, (NameError,))

    @classmethod
    def update_forward_refs(cls, **localns: Any) -> None:
        """
        Try to update ForwardRefs on fields based on this Model, globalns and localns.
        """
        update_model_forward_refs(cls, cls.__fields__.values(), cls.__config__.json_encoders, localns)

    def __iter__(self) -> 'TupleGenerator':
        """
        so `dict(model)` works
        """
        yield from self.__dict__.items()

    def _iter(
        self,
        to_dict: bool = False,
        by_alias: bool = False,
        include: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        exclude: Optional[Union['AbstractSetIntStr', 'MappingIntStrAny']] = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
    ) -> 'TupleGenerator':

        # Merge field set excludes with explicit exclude parameter with explicit overriding field set options.
        # The extra "is not None" guards are not logically necessary but optimizes performance for the simple case.
        # if exclude is not None or self.__exclude_fields__ is not None:
        #     exclude = ValueItems.merge(self.__exclude_fields__, exclude)
        #
        # if include is not None or self.__include_fields__ is not None:
        #     include = ValueItems.merge(self.__include_fields__, include, intersect=True)

        allowed_keys = self._calculate_keys(
            include=include, exclude=exclude, exclude_unset=exclude_unset  # type: ignore
        )
        if allowed_keys is None and not (to_dict or by_alias or exclude_unset or exclude_defaults or exclude_none):
            # huge boost for plain _iter()
            yield from self.__dict__.items()
            return

        value_exclude = ValueItems(self, exclude) if exclude is not None else None
        value_include = ValueItems(self, include) if include is not None else None

        for field_key, v in self.__dict__.items():
            if (allowed_keys is not None and field_key not in allowed_keys) or (exclude_none and v is None):
                continue

            if exclude_defaults:
                model_field = self.__fields__.get(field_key)
                if not getattr(model_field, 'required', True) and getattr(model_field, 'default', _missing) == v:
                    continue

            if by_alias and field_key in self.__fields__:
                dict_key = self.__fields__[field_key].alias
            else:
                dict_key = field_key

            if to_dict or value_include or value_exclude:
                v = self._get_value(
                    v,
                    to_dict=to_dict,
                    by_alias=by_alias,
                    include=value_include and value_include.for_element(field_key),
                    exclude=value_exclude and value_exclude.for_element(field_key),
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                )
            yield dict_key, v

    def _calculate_keys(
        self,
        include: Optional['MappingIntStrAny'],
        exclude: Optional['MappingIntStrAny'],
        exclude_unset: bool,
        update: Optional['DictStrAny'] = None,
    ) -> Optional[AbstractSet[str]]:
        if include is None and exclude is None and exclude_unset is False:
            return None

        keys: AbstractSet[str]
        if exclude_unset:
            keys = self.__fields_set__.copy()
        else:
            keys = self.__dict__.keys()

        if include is not None:
            keys &= include.keys()

        if update:
            keys -= update.keys()

        if exclude:
            keys -= {k for k, v in exclude.items() if ValueItems.is_true(v)}

        return keys

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, BaseModel):
            return self.dict() == other.dict()
        else:
            return self.dict() == other

    def __repr_args__(self) -> 'ReprArgs':
        return [
            (k, v)
            for k, v in self.__dict__.items()
            # if k not in DUNDER_ATTRIBUTES and (k not in self.__fields__ or self.__fields__[k].field_info.repr)
        ]


_base_class_defined = True


@overload
def create_model(
    __model_name: str,
    *,
    __config__: Optional[Type[BaseConfig]] = None,
    __base__: None = None,
    __module__: str = __name__,
    __validators__: Dict[str, 'AnyClassMethod'] = None,
    __cls_kwargs__: Dict[str, Any] = None,
    **field_definitions: Any,
) -> Type['BaseModel']:
    ...


@overload
def create_model(
    __model_name: str,
    *,
    __config__: Optional[Type[BaseConfig]] = None,
    __base__: Union[Type['Model'], Tuple[Type['Model'], ...]],
    __module__: str = __name__,
    __validators__: Dict[str, 'AnyClassMethod'] = None,
    __cls_kwargs__: Dict[str, Any] = None,
    **field_definitions: Any,
) -> Type['Model']:
    ...


def create_model(
    __model_name: str,
    *,
    __config__: Optional[Type[BaseConfig]] = None,
    __base__: Union[None, Type['Model'], Tuple[Type['Model'], ...]] = None,
    __module__: str = __name__,
    __validators__: Dict[str, 'AnyClassMethod'] = None,
    __cls_kwargs__: Dict[str, Any] = None,
    __slots__: Optional[Tuple[str, ...]] = None,
    **field_definitions: Any,
) -> Type['Model']:
    """
    Dynamically create a model.
    :param __model_name: name of the created model
    :param __config__: config class to use for the new model
    :param __base__: base class for the new model to inherit from
    :param __module__: module of the created model
    :param __validators__: a dict of method names and @validator class methods
    :param __cls_kwargs__: a dict for class creation
    :param __slots__: Deprecated, `__slots__` should not be passed to `create_model`
    :param field_definitions: fields of the model (or extra fields if a base is supplied)
        in the format `<name>=(<type>, <default default>)` or `<name>=<default value>, e.g.
        `foobar=(str, ...)` or `foobar=123`, or, for complex use-cases, in the format
        `<name>=<Field>` or `<name>=(<type>, <FieldInfo>)`, e.g.
        `foo=Field(datetime, default_factory=datetime.utcnow, alias='bar')` or
        `foo=(str, FieldInfo(title='Foo'))`
    """
    if __slots__ is not None:
        # __slots__ will be ignored from here on
        warnings.warn('__slots__ should not be passed to create_model', RuntimeWarning)

    if __base__ is not None:
        if __config__ is not None:
            raise ConfigError('to avoid confusion __config__ and __base__ cannot be used together')
        if not isinstance(__base__, tuple):
            __base__ = (__base__,)
    else:
        __base__ = (cast(Type['Model'], BaseModel),)

    __cls_kwargs__ = __cls_kwargs__ or {}

    fields = {}
    annotations = {}

    for f_name, f_def in field_definitions.items():
        if not is_valid_field(f_name):
            warnings.warn(f'fields may not start with an underscore, ignoring "{f_name}"', RuntimeWarning)
        if isinstance(f_def, tuple):
            try:
                f_annotation, f_value = f_def
            except ValueError as e:
                raise ConfigError(
                    'field definitions should either be a tuple of (<type>, <default>) or just a '
                    'default value, unfortunately this means tuples as '
                    'default values are not allowed'
                ) from e
        else:
            f_annotation, f_value = None, f_def

        if f_annotation:
            annotations[f_name] = f_annotation
        fields[f_name] = f_value

    namespace: 'DictStrAny' = {'__annotations__': annotations, '__module__': __module__}
    if __validators__:
        namespace.update(__validators__)
    namespace.update(fields)
    if __config__:
        namespace['Config'] = inherit_config(__config__, BaseConfig)
    resolved_bases = resolve_bases(__base__)
    meta, ns, kwds = prepare_class(__model_name, resolved_bases, kwds=__cls_kwargs__)
    if resolved_bases is not __base__:
        ns['__orig_bases__'] = __base__
    namespace.update(ns)
    return meta(__model_name, resolved_bases, namespace, **kwds)
