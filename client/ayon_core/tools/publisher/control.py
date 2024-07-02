import os
import copy
import logging
import traceback
import collections
import uuid
import tempfile
import shutil
import inspect
from abc import ABCMeta, abstractmethod
import re

import six
import arrow
import pyblish.api
import ayon_api

from ayon_core.lib.events import QueuedEventSystem
from ayon_core.lib.attribute_definitions import (
    UIDef,
    serialize_attr_defs,
    deserialize_attr_defs,
)
from ayon_core.pipeline import (
    PublishValidationError,
    KnownPublishError,
    registered_host,
    get_process_id,
    OptionalPyblishPluginMixin,
)
from ayon_core.pipeline.create import (
    CreateContext,
    AutoCreator,
    HiddenCreator,
    Creator,
)
from ayon_core.pipeline.create.context import (
    CreatorsOperationFailed,
    ConvertorsOperationFailed,
)
from ayon_core.pipeline.publish import get_publish_instance_label
from ayon_core.tools.common_models import ProjectsModel, HierarchyModel
from ayon_core.lib.profiles_filtering import filter_profiles

# Define constant for plugin orders offset
PLUGIN_ORDER_OFFSET = 0.5


class CardMessageTypes:
    standard = None
    info = "info"
    error = "error"


class MainThreadItem:
    """Callback with args and kwargs."""

    def __init__(self, callback, *args, **kwargs):
        self.callback = callback
        self.args = args
        self.kwargs = kwargs

    def process(self):
        self.callback(*self.args, **self.kwargs)


class PublishReportMaker:
    """Report for single publishing process.

    Report keeps current state of publishing and currently processed plugin.
    """

    def __init__(self, controller):
        self.controller = controller
        self._create_discover_result = None
        self._convert_discover_result = None
        self._publish_discover_result = None

        self._plugin_data_by_id = {}
        self._current_plugin = None
        self._current_plugin_data = {}
        self._all_instances_by_id = {}
        self._current_context = None

    def reset(self, context, create_context):
        """Reset report and clear all data."""

        self._create_discover_result = create_context.creator_discover_result
        self._convert_discover_result = (
            create_context.convertor_discover_result
        )
        self._publish_discover_result = create_context.publish_discover_result

        self._plugin_data_by_id = {}
        self._current_plugin = None
        self._current_plugin_data = {}
        self._all_instances_by_id = {}
        self._current_context = context

        for plugin in create_context.publish_plugins_mismatch_targets:
            plugin_data = self._add_plugin_data_item(plugin)
            plugin_data["skipped"] = True

    def add_plugin_iter(self, plugin, context):
        """Add report about single iteration of plugin."""
        for instance in context:
            self._all_instances_by_id[instance.id] = instance

        if self._current_plugin_data:
            self._current_plugin_data["passed"] = True

        self._current_plugin = plugin
        self._current_plugin_data = self._add_plugin_data_item(plugin)

    def _add_plugin_data_item(self, plugin):
        if plugin.id in self._plugin_data_by_id:
            # A plugin would be processed more than once. What can cause it:
            #   - there is a bug in controller
            #   - plugin class is imported into multiple files
            #       - this can happen even with base classes from 'pyblish'
            raise ValueError(
                "Plugin '{}' is already stored".format(str(plugin)))

        plugin_data_item = self._create_plugin_data_item(plugin)
        self._plugin_data_by_id[plugin.id] = plugin_data_item

        return plugin_data_item

    def _create_plugin_data_item(self, plugin):
        label = None
        if hasattr(plugin, "label"):
            label = plugin.label

        return {
            "id": plugin.id,
            "name": plugin.__name__,
            "label": label,
            "order": plugin.order,
            "targets": list(plugin.targets),
            "instances_data": [],
            "action_items": [],
            "actions_data": [],
            "skipped": False,
            "passed": False,
            "errored": False,
            "is_blocking": False,
        }

    def set_plugin_skipped(self):
        """Set that current plugin has been skipped."""
        self._current_plugin_data["skipped"] = True

    def set_plugin_action_items(self, action_items):
        """Set action items for the current plugin.

        This method updates the `action_items` attribute of the currently
        processed plugin with the provided action items.

        Args:
            action_items (list): A list of action items to be associated with
                                 the current plugin.
        """
        self._current_plugin_data["action_items"] = action_items

    def add_result(self, result):
        """Handle result of one plugin and it's instance."""

        instance = result["instance"]
        instance_id = None
        if instance is not None:
            instance_id = instance.id
        self._current_plugin_data["instances_data"].append({
            "id": instance_id,
            "logs": self._extract_instance_log_items(result),
            "process_time": result["duration"]
        })
        self._current_plugin_data["errored"] = result.get(
            "is_validation_error",
            False
        )
        self._current_plugin_data["is_blocking"] = result.get(
            "is_blocking",
            False
        )

    def add_action_result(self, action, result):
        """Add result of single action."""
        plugin = result["plugin"]

        store_item = self._plugin_data_by_id.get(plugin.id)
        if store_item is None:
            store_item = self._add_plugin_data_item(plugin)

        action_name = action.__name__
        action_label = action.label or action_name
        log_items = self._extract_log_items(result)
        store_item["actions_data"].append({
            "success": result["success"],
            "name": action_name,
            "label": action_label,
            "logs": log_items
        })

    def get_report(self, publish_plugins=None):
        """Report data with all details of current state."""

        now = arrow.utcnow().to("local")
        instances_details = {}
        for instance in self._all_instances_by_id.values():
            instances_details[instance.id] = self._extract_instance_data(
                instance, instance in self._current_context
            )

        plugins_data_by_id = copy.deepcopy(
            self._plugin_data_by_id
        )

        # Ensure the current plug-in is marked as `passed` in the result
        # so that it shows on reports for paused publishes
        if self._current_plugin is not None:
            current_plugin_data = plugins_data_by_id.get(
                self._current_plugin.id
            )
            if current_plugin_data and not current_plugin_data["passed"]:
                current_plugin_data["passed"] = True

        if publish_plugins:
            for plugin in publish_plugins:
                if plugin.id not in plugins_data_by_id:
                    plugins_data_by_id[plugin.id] = \
                        self._create_plugin_data_item(plugin)

        reports = []
        if self._create_discover_result is not None:
            reports.append(self._create_discover_result)

        if self._convert_discover_result is not None:
            reports.append(self._convert_discover_result)

        if self._publish_discover_result is not None:
            reports.append(self._publish_discover_result)

        crashed_file_paths = {}
        for report in reports:
            items = report.crashed_file_paths.items()
            for filepath, exc_info in items:
                crashed_file_paths[filepath] = "".join(
                    traceback.format_exception(*exc_info)
                )

        return {
            "plugins_data": list(plugins_data_by_id.values()),
            "instances": instances_details,
            "context": self._extract_context_data(self._current_context),
            "crashed_file_paths": crashed_file_paths,
            "id": uuid.uuid4().hex,
            "created_at": now.isoformat(),
            "report_version": "1.0.1",
        }

    def _extract_context_data(self, context):
        context_label = "Context"
        if context is not None:
            context_label = context.data.get("label")
        return {
            "label": context_label
        }

    def _extract_instance_data(self, instance, exists):
        return {
            "name": instance.data.get("name"),
            "label": get_publish_instance_label(instance),
            "product_type": instance.data.get("productType"),
            "family": instance.data.get("family"),
            "families": instance.data.get("families") or [],
            "exists": exists,
            "creator_identifier": instance.data.get("creator_identifier"),
            "instance_id": instance.data.get("instance_id"),
        }

    def _extract_instance_log_items(self, result):
        instance = result["instance"]
        instance_id = None
        if instance:
            instance_id = instance.id

        log_items = self._extract_log_items(result)
        for item in log_items:
            item["instance_id"] = instance_id
        return log_items

    def _extract_log_items(self, result):
        output = []
        records = result.get("records") or []
        for record in records:
            record_exc_info = record.exc_info
            if record_exc_info is not None:
                record_exc_info = "".join(
                    traceback.format_exception(*record_exc_info)
                )

            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)

            levels = {
                logging.DEBUG: "is_debug",
                logging.INFO: "is_info",
                logging.WARNING: "is_warning",
                logging.ERROR: "is_error",
                logging.CRITICAL: "is_critical"
            }

            log_levels = {level: False for level in levels.values()}
            log_levels[levels.get(record.levelno, "")] = True

            output.append({
                "type": "record",
                **log_levels,
                "msg": msg,
                "name": record.name,
                "lineno": record.lineno,
                "levelno": record.levelno,
                "levelname": record.levelname,
                "threadName": record.threadName,
                "filename": record.filename,
                "pathname": record.pathname,
                "msecs": record.msecs,
                "exc_info": record_exc_info
            })

        exception = result.get("error")
        if exception:
            fname, line_no, func, exc = exception.traceback

            # Conversion of exception into string may crash
            try:
                msg = str(exception)
            except BaseException:
                msg = (
                    "Publisher Controller: ERROR"
                    " - Failed to get exception message"
                )

            # Action result does not have 'is_validation_error'
            is_validation_error = result.get("is_validation_error", False)
            is_blocking = result.get("is_blocking", False)
            output.append({
                "type": "error",
                "is_validation_error": is_validation_error,
                "is_blocking": is_blocking,
                "msg": msg,
                "filename": str(fname),
                "lineno": str(line_no),
                "func": str(func),
                "traceback": exception.formatted_traceback
            })

        return output


class PublishPluginsProxy:
    """Wrapper around publish plugin.

    Prepare mapping for publish plugins and actions. Also can create
    serializable data for plugin actions so UI don't have to have access to
    them.

    This object is created in process where publishing is actually running.

    Notes:
        Actions have id but single action can be used on multiple plugins so
            to run an action is needed combination of plugin and action.

    Args:
        plugins [List[pyblish.api.Plugin]]: Discovered plugins that will be
            processed.
    """

    def __init__(self, plugins):
        self._plugins_by_id = {}
        self._actions_by_plugin_id = {}
        self._action_ids_by_plugin_id = {}

        for plugin in plugins:
            plugin_id = plugin.id
            self._plugins_by_id[plugin_id] = plugin

    def set_plugin_actions(self, plugin_id, exception):
        """ Assign active actions to a plugin.

        Filters and assigns actions that are active for a given plugin,
        considering the current exception.
        The actions are stored and indexed by their IDs for quick access.

        Args:
            plugin_id (str): The identifier for the plugin.
            exception (Type[Exception]): The exception that might influence
            action activation.
        """
        action_ids = []
        actions_by_id = {}

        actions = getattr(self._plugins_by_id[plugin_id], "actions", [])
        filtered_actions = self._filter_plugin_active_actions(actions,
                                                              exception)
        for action in filtered_actions:
            action_id = action.id
            action_ids.append(action_id)
            actions_by_id[action_id] = action

        self._action_ids_by_plugin_id[plugin_id] = action_ids
        self._actions_by_plugin_id[plugin_id] = actions_by_id

    def get_action(self, plugin_id, action_id):
        return self._actions_by_plugin_id[plugin_id][action_id]

    def get_plugin(self, plugin_id):
        return self._plugins_by_id[plugin_id]

    def get_plugin_id(self, plugin):
        """Get id of plugin based on plugin object.

        It's used for validation errors report.

        Args:
            plugin (pyblish.api.Plugin): Publish plugin for which id should be
                returned.

        Returns:
            str: Plugin id.
        """

        return plugin.id

    def get_plugin_action_items(self, plugin_id):
        """Get plugin action items for plugin by its id.

        Args:
            plugin_id (str): Publish plugin id.

        Returns:
            List[PublishPluginActionItem]: Items with information about publish
                plugin actions.
        """

        return [
            self._create_action_item(
                self.get_action(plugin_id, action_id), plugin_id
            )
            for action_id in self._action_ids_by_plugin_id[plugin_id]
        ]

    def _create_action_item(self, action, plugin_id):
        label = action.label or action.__name__
        icon = getattr(action, "icon", None)
        return PublishPluginActionItem(
            action.id,
            plugin_id,
            action.active,
            action.on,
            label,
            icon
        )

    @staticmethod
    def _filter_plugin_active_actions(actions, exception):
        """ Filters active actions based on the plugin's specified conditions.

        Determines which actions are active depending on the plugin's
        specified conditions and the type and severity of the exception.

        Args:
            actions (List[Action]): A list of Action objects to filter.
            exception (Type[Exception]): The exception raised during plugin
            execution.

        Returns:
            List[Action]: A list of actions that are active based on the
            specified conditions.

        Raises:
            None
        """
        plugin_errored = isinstance(exception, PublishValidationError)
        error_is_blocking = getattr(exception, "is_blocking", False)

        def is_action_active(action):
            """ Evaluate if an action should be activated.

            Checks whether an action meets its specified conditions for
            activation, which can include being always active, or active in
            response to specific states of the plugin.

            Args:
                action (Action): The action to evaluate.

            Returns:
                bool: Indicates if the action meets the activation criteria.
            """
            return action.active and (
                    action.on == "all" or
                    (action.on == "failedOrWarning" and plugin_errored) or
                    (action.on == "failed" and
                     (
                             plugin_errored and error_is_blocking)
                     )
            )

        return list(filter(is_action_active, actions))


class PublishPluginActionItem:
    """Representation of publish plugin action.

    Data driven object which is used as proxy for controller and UI.

    Args:
        action_id (str): Action id.
        plugin_id (str): Plugin id.
        active (bool): Action is active.
        on_filter (str): Actions have 'on' attribte which define when can be
            action triggered (e.g. 'all', 'failed', ...).
        label (str): Action's label.
        icon (Union[str, None]) Action's icon.
    """

    def __init__(self, action_id, plugin_id, active, on_filter, label, icon):
        self.action_id = action_id
        self.plugin_id = plugin_id
        self.active = active
        self.on_filter = on_filter
        self.label = label
        self.icon = icon

    def to_data(self):
        """Serialize object to dictionary.

        Returns:
            Dict[str, Union[str,bool,None]]: Serialized object.
        """

        return {
            "action_id": self.action_id,
            "plugin_id": self.plugin_id,
            "active": self.active,
            "on_filter": self.on_filter,
            "label": self.label,
            "icon": self.icon
        }

    @classmethod
    def from_data(cls, data):
        """Create object from data.

        Args:
            data (Dict[str, Union[str,bool,None]]): Data used to recreate
                object.

        Returns:
            PublishPluginActionItem: Object created using data.
        """

        return cls(**data)


class ValidationErrorItem:
    """Data driven validation error item.

    Prepared data container with information about validation error and it's
    source plugin.

    Can be converted to raw data and recreated should be used for controller
    and UI connection.

    Args:
        instance_id (str): Id of pyblish instance to which is validation error
            connected.
        instance_label (str): Prepared instance label.
        plugin_id (str): Id of pyblish Plugin which triggered the validation
            error. Id is generated using 'PublishPluginsProxy'.
    """

    def __init__(
        self,
        instance_id,
        instance_label,
        plugin_id,
        context_validation,
        title,
        description,
        detail,
        is_blocking
    ):
        self.instance_id = instance_id
        self.instance_label = instance_label
        self.plugin_id = plugin_id
        self.context_validation = context_validation
        self.title = title
        self.description = description
        self.detail = detail
        self.is_blocking = is_blocking

    def to_data(self):
        """Serialize object to dictionary.

        Returns:
            Dict[str, Union[str, bool, None]]: Serialized object data.
        """

        return {
            "instance_id": self.instance_id,
            "instance_label": self.instance_label,
            "plugin_id": self.plugin_id,
            "context_validation": self.context_validation,
            "title": self.title,
            "description": self.description,
            "detail": self.detail,
            "is_blocking": self.is_blocking,
        }

    @classmethod
    def from_result(cls, plugin_id, error, instance):
        """Create new object based on resukt from controller.

        Returns:
            ValidationErrorItem: New object with filled data.
        """

        instance_label = None
        instance_id = None
        if instance is not None:
            instance_label = (
                instance.data.get("label") or instance.data.get("name")
            )
            instance_id = instance.id

        return cls(
            instance_id,
            instance_label,
            plugin_id,
            instance is None,
            error.title,
            error.description,
            error.detail,
            error.is_blocking
        )

    @classmethod
    def from_data(cls, data):
        return cls(**data)


class PublishValidationErrorsReport:
    """Publish validation errors report that can be parsed to raw data.

    Args:
        error_items (List[ValidationErrorItem]): List of validation errors.
        plugin_action_items (Dict[str, PublishPluginActionItem]): Action items
            by plugin id.
    """

    def __init__(self, error_items, plugin_action_items):
        self._error_items = error_items
        self._plugin_action_items = plugin_action_items
        self.has_blocking_errors = False
        self.has_non_blocking_errors = False

        for error in self._error_items:
            if error.is_blocking:
                self.has_blocking_errors = True
            else:
                self.has_non_blocking_errors = True

            if self.has_blocking_errors and self.has_non_blocking_errors:
                break

    def __iter__(self):
        for item in self._error_items:
            yield item

    def group_items_by_title(self):
        """Group errors by plugin and their titles.

        Items are grouped by plugin and title -> same title from different
        plugin is different item. Items are ordered by plugin order.

        Returns:
            List[Dict[str, Any]]: List where each item title, instance
                information related to title and possible plugin actions.
        """

        ordered_plugin_ids = []
        error_items_by_plugin_id = collections.defaultdict(list)
        for error_item in self._error_items:
            plugin_id = error_item.plugin_id
            if plugin_id not in ordered_plugin_ids:
                ordered_plugin_ids.append(plugin_id)
            error_items_by_plugin_id[plugin_id].append(error_item)

        grouped_error_items = []
        for plugin_id in ordered_plugin_ids:
            plugin_action_items = self._plugin_action_items[plugin_id]
            error_items = error_items_by_plugin_id[plugin_id]

            titles = []
            error_items_by_title = collections.defaultdict(list)
            for error_item in error_items:
                title = error_item.title
                if title not in titles:
                    titles.append(error_item.title)
                error_items_by_title[title].append(error_item)

            for title in titles:
                grouped_error_items.append({
                    "id": uuid.uuid4().hex,
                    "plugin_id": plugin_id,
                    "plugin_action_items": list(plugin_action_items),
                    "error_items": error_items_by_title[title],
                    "title": title
                })
        return grouped_error_items

    def to_data(self):
        """Serialize object to dictionary.

        Returns:
            Dict[str, Any]: Serialized data.
        """

        error_items = [
            item.to_data()
            for item in self._error_items
        ]

        plugin_action_items = {
            plugin_id: [
                action_item.to_data()
                for action_item in action_items
            ]
            for plugin_id, action_items in self._plugin_action_items.items()
        }

        return {
            "error_items": error_items,
            "plugin_action_items": plugin_action_items
        }

    @classmethod
    def from_data(cls, data):
        """Recreate object from data.

        Args:
            data (dict[str, Any]): Data to recreate object. Can be created
                using 'to_data' method.

        Returns:
            PublishValidationErrorsReport: New object based on data.
        """

        error_items = [
            ValidationErrorItem.from_data(error_item)
            for error_item in data["error_items"]
        ]
        plugin_action_items = [
            PublishPluginActionItem.from_data(action_item)
            for action_item in data["plugin_action_items"]
        ]
        return cls(error_items, plugin_action_items)


class PublishValidationErrors:
    """Object to keep track about validation errors by plugin."""

    def __init__(self):
        self._plugins_proxy = None
        self._error_items = []
        self._plugin_action_items = {}

    def __bool__(self):
        return self.has_errors

    @property
    def has_errors(self):
        """At least one error was added."""

        return bool(self._error_items)

    def reset(self, plugins_proxy):
        """Reset object to default state.

        Args:
            plugins_proxy (PublishPluginsProxy): Proxy which store plugins,
                actions by ids and create mapping of action ids by plugin ids.
        """

        self._plugins_proxy = plugins_proxy
        self._error_items = []
        self._plugin_action_items = {}

    def create_report(self):
        """Create report based on currently existing errors.

        Returns:
            PublishValidationErrorsReport: Validation error report with all
                error information and publish plugin action items.
        """

        return PublishValidationErrorsReport(
            self._error_items, self._plugin_action_items
        )

    def add_error(self, plugin, error, instance):
        """Add error from pyblish result.

        Args:
            plugin (pyblish.api.Plugin): Plugin which triggered error.
            error (ValidationException): Validation error.
            instance (Union[pyblish.api.Instance, None]): Instance on which was
                error raised or None if was raised on context.
        """

        # Make sure the cached report is cleared
        plugin_id = self._plugins_proxy.get_plugin_id(plugin)
        if not error.title:
            if hasattr(plugin, "label") and plugin.label:
                plugin_label = plugin.label
            else:
                plugin_label = plugin.__name__
            error.title = plugin_label

        self._error_items.append(
            ValidationErrorItem.from_result(plugin_id, error, instance)
        )
        if plugin_id in self._plugin_action_items:
            return

        plugin_actions = self._plugins_proxy.get_plugin_action_items(
            plugin_id
        )
        self._plugin_action_items[plugin_id] = plugin_actions


class CreatorType:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name

    def __eq__(self, other):
        return self.name == str(other)

    def __ne__(self, other):
        # This is implemented only because of Python 2
        return not self == other


class CreatorTypes:
    base = CreatorType("base")
    auto = CreatorType("auto")
    hidden = CreatorType("hidden")
    artist = CreatorType("artist")

    @classmethod
    def from_str(cls, value):
        for creator_type in (
            cls.base,
            cls.auto,
            cls.hidden,
            cls.artist
        ):
            if value == creator_type:
                return creator_type
        raise ValueError("Unknown type \"{}\"".format(str(value)))


class CreatorItem:
    """Wrapper around Creator plugin.

    Object can be serialized and recreated.
    """

    def __init__(
        self,
        identifier,
        creator_type,
        product_type,
        label,
        group_label,
        icon,
        description,
        detailed_description,
        default_variant,
        default_variants,
        create_allow_context_change,
        create_allow_thumbnail,
        show_order,
        pre_create_attributes_defs,
    ):
        self.identifier = identifier
        self.creator_type = creator_type
        self.product_type = product_type
        self.label = label
        self.group_label = group_label
        self.icon = icon
        self.description = description
        self.detailed_description = detailed_description
        self.default_variant = default_variant
        self.default_variants = default_variants
        self.create_allow_context_change = create_allow_context_change
        self.create_allow_thumbnail = create_allow_thumbnail
        self.show_order = show_order
        self.pre_create_attributes_defs = pre_create_attributes_defs

    def get_group_label(self):
        return self.group_label

    @classmethod
    def from_creator(cls, creator):
        if isinstance(creator, AutoCreator):
            creator_type = CreatorTypes.auto
        elif isinstance(creator, HiddenCreator):
            creator_type = CreatorTypes.hidden
        elif isinstance(creator, Creator):
            creator_type = CreatorTypes.artist
        else:
            creator_type = CreatorTypes.base

        description = None
        detail_description = None
        default_variant = None
        default_variants = None
        pre_create_attr_defs = None
        create_allow_context_change = None
        create_allow_thumbnail = None
        show_order = creator.order
        if creator_type is CreatorTypes.artist:
            description = creator.get_description()
            detail_description = creator.get_detail_description()
            default_variant = creator.get_default_variant()
            default_variants = creator.get_default_variants()
            pre_create_attr_defs = creator.get_pre_create_attr_defs()
            create_allow_context_change = creator.create_allow_context_change
            create_allow_thumbnail = creator.create_allow_thumbnail
            show_order = creator.show_order

        identifier = creator.identifier
        return cls(
            identifier,
            creator_type,
            creator.product_type,
            creator.label or identifier,
            creator.get_group_label(),
            creator.get_icon(),
            description,
            detail_description,
            default_variant,
            default_variants,
            create_allow_context_change,
            create_allow_thumbnail,
            show_order,
            pre_create_attr_defs,
        )

    def to_data(self):
        pre_create_attributes_defs = None
        if self.pre_create_attributes_defs is not None:
            pre_create_attributes_defs = serialize_attr_defs(
                self.pre_create_attributes_defs
            )

        return {
            "identifier": self.identifier,
            "creator_type": str(self.creator_type),
            "product_type": self.product_type,
            "label": self.label,
            "group_label": self.group_label,
            "icon": self.icon,
            "description": self.description,
            "detailed_description": self.detailed_description,
            "default_variant": self.default_variant,
            "default_variants": self.default_variants,
            "create_allow_context_change": self.create_allow_context_change,
            "create_allow_thumbnail": self.create_allow_thumbnail,
            "show_order": self.show_order,
            "pre_create_attributes_defs": pre_create_attributes_defs,
        }

    @classmethod
    def from_data(cls, data):
        pre_create_attributes_defs = data["pre_create_attributes_defs"]
        if pre_create_attributes_defs is not None:
            data["pre_create_attributes_defs"] = deserialize_attr_defs(
                pre_create_attributes_defs
            )

        data["creator_type"] = CreatorTypes.from_str(data["creator_type"])
        return cls(**data)


@six.add_metaclass(ABCMeta)
class AbstractPublisherController(object):
    """Publisher tool controller.

    Define what must be implemented to be able use Publisher functionality.

    Goal is to have "data driven" controller that can be used to control UI
    running in different process. That lead to some disadvantages like UI can't
    access objects directly but by using wrappers that can be serialized.
    """

    @property
    @abstractmethod
    def log(self):
        """Controller's logger object.

        Returns:
            logging.Logger: Logger object that can be used for logging.
        """

        pass

    @property
    @abstractmethod
    def event_system(self):
        """Inner event system for publisher controller."""

        pass

    @property
    @abstractmethod
    def project_name(self):
        """Current context project name.

        Returns:
            str: Name of project.
        """

        pass

    @property
    @abstractmethod
    def current_folder_path(self):
        """Current context folder path.

        Returns:
            Union[str, None]: Folder path.

        """
        pass

    @property
    @abstractmethod
    def current_task_name(self):
        """Current context task name.

        Returns:
            Union[str, None]: Name of task.
        """

        pass

    @property
    @abstractmethod
    def host_context_has_changed(self):
        """Host context changed after last reset.

        'CreateContext' has this option available using 'context_has_changed'.

        Returns:
            bool: Context has changed.
        """

        pass

    @property
    @abstractmethod
    def host_is_valid(self):
        """Host is valid for creation part.

        Host must have implemented certain functionality to be able create
        in Publisher tool.

        Returns:
            bool: Host can handle creation of instances.
        """

        pass

    @property
    @abstractmethod
    def instances(self):
        """Collected/created instances.

        Returns:
            List[CreatedInstance]: List of created instances.
        """

        pass

    @abstractmethod
    def get_context_title(self):
        """Get context title for artist shown at the top of main window.

        Returns:
            Union[str, None]: Context title for window or None. In case of None
                a warning is displayed (not nice for artists).
        """

        pass

    @abstractmethod
    def get_existing_product_names(self, folder_path):
        pass

    @abstractmethod
    def reset(self):
        """Reset whole controller.

        This should reset create context, publish context and all variables
        that are related to it.
        """

        pass

    @abstractmethod
    def get_creator_attribute_definitions(self, instances):
        pass

    @abstractmethod
    def get_publish_attribute_definitions(self, instances, include_context):
        pass

    @abstractmethod
    def get_creator_icon(self, identifier):
        """Receive creator's icon by identifier.

        Args:
            identifier (str): Creator's identifier.

        Returns:
            Union[str, None]: Creator's icon string.
        """

        pass

    @abstractmethod
    def get_product_name(
        self,
        creator_identifier,
        variant,
        task_name,
        folder_path,
        instance_id=None
    ):
        """Get product name based on passed data.

        Args:
            creator_identifier (str): Identifier of creator which should be
                responsible for product name creation.
            variant (str): Variant value from user's input.
            task_name (str): Name of task for which is instance created.
            folder_path (str): Folder path for which is instance created.
            instance_id (Union[str, None]): Existing instance id when product
                name is updated.
        """

        pass

    @abstractmethod
    def create(
        self, creator_identifier, product_name, instance_data, options
    ):
        """Trigger creation by creator identifier.

        Should also trigger refresh of instanes.

        Args:
            creator_identifier (str): Identifier of Creator plugin.
            product_name (str): Calculated product name.
            instance_data (Dict[str, Any]): Base instance data with variant,
                folder path and task name.
            options (Dict[str, Any]): Data from pre-create attributes.
        """

        pass

    @abstractmethod
    def save_changes(self):
        """Save changes in create context.

        Save can crash because of unexpected errors.

        Returns:
            bool: Save was successful.
        """

        pass

    @abstractmethod
    def remove_instances(self, instance_ids):
        """Remove list of instances from create context."""
        # TODO expect instance ids

        pass

    @property
    @abstractmethod
    def publish_has_started(self):
        """Has publishing finished.

        Returns:
            bool: If publishing finished and all plugins were iterated.
        """

        pass

    @property
    @abstractmethod
    def publish_has_finished(self):
        """Has publishing finished.

        Returns:
            bool: If publishing finished and all plugins were iterated.
        """

        pass

    @property
    @abstractmethod
    def publish_is_running(self):
        """Publishing is running right now.

        Returns:
            bool: If publishing is in progress.
        """

        pass

    @property
    @abstractmethod
    def publish_has_validated(self):
        """Publish validation passed.

        Returns:
            bool: If publishing passed last possible validation order.
        """

        pass

    @property
    @abstractmethod
    def publish_has_crashed(self):
        """Publishing crashed for any reason.

        Returns:
            bool: Publishing crashed.
        """

        pass

    @property
    @abstractmethod
    def publish_has_validation_errors(self):
        """During validation happened at least one validation error.

        Returns:
            bool: Validation error was raised during validation.
        """

        pass

    @property
    @abstractmethod
    def publish_has_validation_blocking_errors(self):
        """During validation happened at least one validation blocking error.

        Returns:
            bool: Validation blocking error was raised during validation.
        """

        pass

    @property
    @abstractmethod
    def publish_max_progress(self):
        """Get maximum possible progress number.

        Returns:
            int: Number that can be used as 100% of publish progress bar.
        """

        pass

    @property
    @abstractmethod
    def publish_progress(self):
        """Current progress number.

        Returns:
            int: Current progress value from 0 to 'publish_max_progress'.
        """

        pass

    @property
    @abstractmethod
    def publish_error_msg(self):
        """Current error message which cause fail of publishing.

        Returns:
            Union[str, None]: Message which will be showed to artist or
                None.
        """

        pass

    @abstractmethod
    def get_publish_report(self):
        pass

    @abstractmethod
    def get_validation_errors(self):
        pass

    @abstractmethod
    def publish(self):
        """Trigger publishing without any order limitations."""

        pass

    @abstractmethod
    def validate(self):
        """Trigger publishing which will stop after validation order."""

        pass

    @abstractmethod
    def stop_publish(self):
        """Stop publishing can be also used to pause publishing.

        Pause of publishing is possible only if all plugins successfully
        finished.
        """

        pass

    @abstractmethod
    def run_action(self, plugin_id, action_id):
        """Trigger pyblish action on a plugin.

        Args:
            plugin_id (str): Id of publish plugin.
            action_id (str): Id of publish action.
        """

        pass

    @property
    @abstractmethod
    def convertor_items(self):
        pass

    @abstractmethod
    def trigger_convertor_items(self, convertor_identifiers):
        pass

    @abstractmethod
    def get_thumbnail_paths_for_instances(self, instance_ids):
        pass

    @abstractmethod
    def set_thumbnail_paths_for_instances(self, thumbnail_path_mapping):
        pass

    @abstractmethod
    def set_comment(self, comment):
        """Set comment on pyblish context.

        Set "comment" key on current pyblish.api.Context data.

        Args:
            comment (str): Artist's comment.
        """

        pass

    @abstractmethod
    def emit_card_message(
        self, message, message_type=CardMessageTypes.standard
    ):
        """Emit a card message which can have a lifetime.

        This is for UI purposes. Method can be extended to more arguments
        in future e.g. different message timeout or type (color).

        Args:
            message (str): Message that will be showed.
        """

        pass

    @abstractmethod
    def get_thumbnail_temp_dir_path(self):
        """Return path to directory where thumbnails can be temporary stored.

        Returns:
            str: Path to a directory.
        """

        pass

    @abstractmethod
    def clear_thumbnail_temp_dir_path(self):
        """Remove content of thumbnail temp directory."""

        pass


class BasePublisherController(AbstractPublisherController):
    """Implement common logic for controllers.

    Implement event system, logger and common attributes. Attributes are
    triggering value changes so anyone can listen to their topics.

    Prepare implementation for creator items. Controller must implement just
    their filling by '_collect_creator_items'.

    All prepared implementation is based on calling super '__init__'.
    """

    def __init__(self):
        self._log = None
        self._event_system = None

        # Host is valid for creation
        self._host_is_valid = False

        # Any other exception that happened during publishing
        self._publish_error_msg = None
        # Publishing is in progress
        self._publish_is_running = False
        # Publishing is over validation order
        self._publish_has_validated = False

        self._publish_has_validation_errors = False
        self._publish_has_validation_blocking_errors = False
        self._publish_has_crashed = False
        # All publish plugins are processed
        self._publish_has_started = False
        self._publish_has_finished = False
        self._publish_max_progress = 0
        self._publish_progress = 0

        # Controller must '_collect_creator_items' to fill the value
        self._creator_items = None

    @property
    def log(self):
        """Controller's logger object.

        Returns:
            logging.Logger: Logger object that can be used for logging.
        """

        if self._log is None:
            self._log = logging.getLogger(self.__class__.__name__)
        return self._log

    @property
    def event_system(self):
        """Inner event system for publisher controller.

        Is used for communication with UI. Event system is autocreated.

        Known topics:
            "show.detailed.help" - Detailed help requested (UI related).
            "show.card.message" - Show card message request (UI related).
            "instances.refresh.finished" - Instances are refreshed.
            "plugins.refresh.finished" - Plugins refreshed.
            "publish.reset.finished" - Reset finished.
            "controller.reset.started" - Controller reset started.
            "controller.reset.finished" - Controller reset finished.
            "publish.process.started" - Publishing started. Can be started from
                paused state.
            "publish.process.stopped" - Publishing stopped/paused process.
            "publish.process.plugin.changed" - Plugin state has changed.
            "publish.process.instance.changed" - Instance state has changed.
            "publish.has_validated.changed" - Attr 'publish_has_validated'
                changed.
            "publish.is_running.changed" - Attr 'publish_is_running' changed.
            "publish.has_crashed.changed" - Attr 'publish_has_crashed' changed.
            "publish.publish_error.changed" - Attr 'publish_error'
            "publish.has_validation_errors.changed" - Attr
                'has_validation_errors' changed.
            "publish.max_progress.changed" - Attr 'publish_max_progress'
                changed.
            "publish.progress.changed" - Attr 'publish_progress' changed.
            "publish.host_is_valid.changed" - Attr 'host_is_valid' changed.
            "publish.finished.changed" - Attr 'publish_has_finished' changed.

        Returns:
            EventSystem: Event system which can trigger callbacks for topics.
        """

        if self._event_system is None:
            self._event_system = QueuedEventSystem()
        return self._event_system

    # Events system
    def emit_event(self, topic, data=None, source=None):
        """Use implemented event system to trigger event."""

        if data is None:
            data = {}
        self.event_system.emit(topic, data, source)

    def register_event_callback(self, topic, callback):
        self.event_system.add_callback(topic, callback)

    def _emit_event(self, topic, data=None):
        self.emit_event(topic, data, "controller")

    def _get_host_is_valid(self):
        return self._host_is_valid

    def _set_host_is_valid(self, value):
        if self._host_is_valid != value:
            self._host_is_valid = value
            self._emit_event(
                "publish.host_is_valid.changed", {"value": value}
            )

    def _get_publish_has_started(self):
        return self._publish_has_started

    def _set_publish_has_started(self, value):
        if value != self._publish_has_started:
            self._publish_has_started = value

    def _get_publish_has_finished(self):
        return self._publish_has_finished

    def _set_publish_has_finished(self, value):
        if self._publish_has_finished != value:
            self._publish_has_finished = value
            self._emit_event("publish.finished.changed", {"value": value})

    def _get_publish_is_running(self):
        return self._publish_is_running

    def _set_publish_is_running(self, value):
        if self._publish_is_running != value:
            self._publish_is_running = value
            self._emit_event("publish.is_running.changed", {"value": value})

    def _get_publish_has_validated(self):
        return self._publish_has_validated

    def _set_publish_has_validated(self, value):
        if self._publish_has_validated != value:
            self._publish_has_validated = value
            self._emit_event(
                "publish.has_validated.changed", {"value": value}
            )

    def _get_publish_has_crashed(self):
        return self._publish_has_crashed

    def _set_publish_has_crashed(self, value):
        if self._publish_has_crashed != value:
            self._publish_has_crashed = value
            self._emit_event("publish.has_crashed.changed", {"value": value})

    def _get_publish_has_validation_errors(self):
        return self._publish_has_validation_errors

    def _get_publish_has_validation_blocking_errors(self):
        return self._publish_has_validation_blocking_errors

    def _set_publish_has_validation_errors(self, value):
        if self._publish_has_validation_errors != value:
            self._publish_has_validation_errors = value
            self._emit_event(
                "publish.has_validation_errors.changed",
                {"value": value}
            )

    def _set_publish_has_validation_blocking_errors(self, value):
        if self._publish_has_validation_blocking_errors != value:
            self._publish_has_validation_blocking_errors = value
            self._emit_event(
                "publish.has_validation_blocking_errors.changed",
                {"value": value}
            )

    def _get_publish_max_progress(self):
        return self._publish_max_progress

    def _set_publish_max_progress(self, value):
        if self._publish_max_progress != value:
            self._publish_max_progress = value
            self._emit_event("publish.max_progress.changed", {"value": value})

    def _get_publish_progress(self):
        return self._publish_progress

    def _set_publish_progress(self, value):
        if self._publish_progress != value:
            self._publish_progress = value
            self._emit_event("publish.progress.changed", {"value": value})

    def _get_publish_error_msg(self):
        return self._publish_error_msg

    def _set_publish_error_msg(self, value):
        if self._publish_error_msg != value:
            self._publish_error_msg = value
            self._emit_event("publish.publish_error.changed", {"value": value})

    host_is_valid = property(
        _get_host_is_valid, _set_host_is_valid
    )
    publish_has_started = property(
        _get_publish_has_started, _set_publish_has_started
    )
    publish_has_finished = property(
        _get_publish_has_finished, _set_publish_has_finished
    )
    publish_is_running = property(
        _get_publish_is_running, _set_publish_is_running
    )
    publish_has_validated = property(
        _get_publish_has_validated, _set_publish_has_validated
    )
    publish_has_crashed = property(
        _get_publish_has_crashed, _set_publish_has_crashed
    )
    publish_has_validation_errors = property(
        _get_publish_has_validation_errors, _set_publish_has_validation_errors
    )
    publish_has_validation_blocking_errors = property(
        _get_publish_has_validation_blocking_errors, _set_publish_has_validation_blocking_errors
    )
    publish_max_progress = property(
        _get_publish_max_progress, _set_publish_max_progress
    )
    publish_progress = property(
        _get_publish_progress, _set_publish_progress
    )
    publish_error_msg = property(
        _get_publish_error_msg, _set_publish_error_msg
    )

    def _reset_attributes(self):
        """Reset most of attributes that can be reset."""

        self.publish_is_running = False
        self.publish_has_started = False
        self.publish_has_validated = False
        self.publish_has_crashed = False
        self.publish_has_validation_errors = False
        self.publish_has_validation_blocking_errors = False
        self.publish_has_finished = False

        self.publish_error_msg = None
        self.publish_progress = 0

    @property
    def creator_items(self):
        """Creators that can be shown in create dialog."""
        if self._creator_items is None:
            self._creator_items = self._collect_creator_items()
        return self._creator_items

    @abstractmethod
    def _collect_creator_items(self):
        """Receive CreatorItems to work with.

        Returns:
            Dict[str, CreatorItem]: Creator items by their identifier.
        """

        pass

    def get_creator_icon(self, identifier):
        """Function to receive icon for creator identifier.

        Args:
            str: Creator's identifier for which should be icon returned.
        """

        creator_item = self.creator_items.get(identifier)
        if creator_item is not None:
            return creator_item.icon
        return None

    def get_thumbnail_temp_dir_path(self):
        """Return path to directory where thumbnails can be temporary stored.

        Returns:
            str: Path to a directory.
        """

        return os.path.join(
            tempfile.gettempdir(),
            "publisher_thumbnails",
            get_process_id()
        )

    def clear_thumbnail_temp_dir_path(self):
        """Remove content of thumbnail temp directory."""

        dirpath = self.get_thumbnail_temp_dir_path()
        if os.path.exists(dirpath):
            shutil.rmtree(dirpath)


class PublisherController(BasePublisherController):
    """Middleware between UI, CreateContext and publish Context.

    Handle both creation and publishing parts.

    Args:
        headless (bool): Headless publishing. ATM not implemented or used.
    """

    _log = None

    def __init__(self, headless=False):
        super(PublisherController, self).__init__()

        self._host = registered_host()
        self._headless = headless

        self._create_context = CreateContext(
            self._host, headless=headless, reset=False
        )

        self._publish_plugins_proxy = None

        # pyblish.api.Context
        self._publish_context = None
        # Pyblish report
        self._publish_report = PublishReportMaker(self)
        # Store exceptions of validation error
        self._publish_validation_errors = PublishValidationErrors()

        # Publishing should stop at validation stage
        self._publish_up_validation = False
        # This information is not much important for controller but for widget
        #   which can change (and set) the comment.
        self._publish_comment_is_set = False

        # Validation order
        # - plugin with order same or higher than this value is extractor or
        #   higher
        self._validation_order = (
            pyblish.api.ValidatorOrder + PLUGIN_ORDER_OFFSET
        )

        # Plugin iterator
        self._main_thread_iter = None

        # State flags to prevent executing method which is already in progress
        self._resetting_plugins = False
        self._resetting_instances = False

        # Cacher of avalon documents
        self._projects_model = ProjectsModel(self)
        self._hierarchy_model = HierarchyModel(self)

        self._ignore_non_blocking_errors = False
        self._btn_clicked = None

    @property
    def project_name(self):
        """Current project context defined by host.

        Returns:
            str: Project name.
        """

        return self._create_context.get_current_project_name()

    @property
    def current_folder_path(self):
        """Current context folder path defined by host.

        Returns:
            Union[str, None]: Folder path or None if folder is not set.
        """

        return self._create_context.get_current_folder_path()

    @property
    def current_task_name(self):
        """Current context task name defined by host.

        Returns:
            Union[str, None]: Task name or None if task is not set.
        """

        return self._create_context.get_current_task_name()

    @property
    def host_context_has_changed(self):
        return self._create_context.context_has_changed

    @property
    def instances(self):
        """Current instances in create context."""
        return self._create_context.instances_by_id

    @property
    def convertor_items(self):
        return self._create_context.convertor_items_by_id

    @property
    def _creators(self):
        """All creators loaded in create context."""

        return self._create_context.creators

    @property
    def _publish_plugins(self):
        """Publish plugins."""
        return self._create_context.publish_plugins

    def _get_current_project_settings(self):
        """Current project settings.

        Returns:
            dict
        """

        return self._create_context.get_current_project_settings()

    def get_folder_type_items(self, project_name, sender=None):
        return self._projects_model.get_folder_type_items(
            project_name, sender
        )

    def get_task_type_items(self, project_name, sender=None):
        return self._projects_model.get_task_type_items(
            project_name, sender
        )

    # Hierarchy model
    def get_folder_items(self, project_name, sender=None):
        return self._hierarchy_model.get_folder_items(project_name, sender)

    def get_task_items(self, project_name, folder_id, sender=None):
        return self._hierarchy_model.get_task_items(
            project_name, folder_id, sender
        )

    def get_folder_entity(self, project_name, folder_id):
        return self._hierarchy_model.get_folder_entity(
            project_name, folder_id
        )

    def get_task_entity(self, project_name, task_id):
        return self._hierarchy_model.get_task_entity(project_name, task_id)

    # Publisher custom method
    def get_folder_id_from_path(self, folder_path):
        if not folder_path:
            return None
        folder_item = self._hierarchy_model.get_folder_item_by_path(
            self.project_name, folder_path
        )
        if folder_item:
            return folder_item.entity_id
        return None

    def get_task_items_by_folder_paths(self, folder_paths):
        if not folder_paths:
            return {}
        folder_items = self._hierarchy_model.get_folder_items_by_paths(
            self.project_name, folder_paths
        )
        output = {
            folder_path: []
            for folder_path in folder_paths
        }
        project_name = self.project_name
        for folder_item in folder_items.values():
            task_items = self._hierarchy_model.get_task_items(
                project_name, folder_item.entity_id, None
            )
            output[folder_item.path] = task_items

        return output

    def are_folder_paths_valid(self, folder_paths):
        if not folder_paths:
            return True
        folder_paths = set(folder_paths)
        folder_items = self._hierarchy_model.get_folder_items_by_paths(
            self.project_name, folder_paths
        )
        for folder_item in folder_items.values():
            if folder_item is None:
                return False
        return True

    # --- Publish specific callbacks ---
    def get_context_title(self):
        """Get context title for artist shown at the top of main window."""

        context_title = None
        if hasattr(self._host, "get_context_title"):
            context_title = self._host.get_context_title()

        if context_title is None:
            context_title = os.environ.get("AYON_APP_NAME")
            if context_title is None:
                context_title = os.environ.get("AYON_HOST_NAME")

        return context_title

    def get_existing_product_names(self, folder_path):
        if not folder_path:
            return None
        project_name = self.project_name
        folder_item = self._hierarchy_model.get_folder_item_by_path(
            project_name, folder_path
        )
        if not folder_item:
            return None

        product_entities = ayon_api.get_products(
            project_name,
            folder_ids={folder_item.entity_id},
            fields={"name"}
        )
        return {
            product_entity["name"]
            for product_entity in product_entities
        }

    def reset(self):
        """Reset everything related to creation and publishing."""
        self.stop_publish()

        self._emit_event("controller.reset.started")

        self.host_is_valid = self._create_context.host_is_valid

        self._create_context.reset_preparation()

        # Reset current context
        self._create_context.reset_current_context()

        self._hierarchy_model.reset()

        self._reset_plugins()
        # Publish part must be reset after plugins
        self._reset_publish()
        self._reset_instances()

        self._create_context.reset_finalization()

        self._emit_event("controller.reset.finished")

        self.emit_card_message("Refreshed..")

    def _reset_plugins(self):
        """Reset to initial state."""
        if self._resetting_plugins:
            return

        self._resetting_plugins = True

        self._create_context.reset_plugins()
        # Reset creator items
        self._creator_items = None

        self._resetting_plugins = False

        self._emit_event("plugins.refresh.finished")

    def _collect_creator_items(self):
        # TODO add crashed initialization of create plugins to report
        output = {}
        allowed_creator_pattern = self._get_allowed_creators_pattern()
        for identifier, creator in self._create_context.creators.items():
            try:
                if (not self._is_label_allowed(
                        creator.label, allowed_creator_pattern)):
                    self.log.debug(f"{creator.label} not allowed for context")
                    continue
                output[identifier] = CreatorItem.from_creator(creator)
            except Exception:
                self.log.error(
                    "Failed to create creator item for '%s'",
                    identifier,
                    exc_info=True
                )

        return output

    def _get_allowed_creators_pattern(self):
        """Provide regex pattern for configured creator labels in this context

        If no profile matches current context, it shows all creators.
        Support usage of regular expressions for configured values.
        Returns:
            (re.Pattern)[optional]: None or regex compiled patterns
                into single one ('Render|Image.*')
        """

        task_type = self._create_context.get_current_task_type()
        project_settings = self._get_current_project_settings()

        filter_creator_profiles = (
            project_settings
            ["core"]
            ["tools"]
            ["creator"]
            ["filter_creator_profiles"]
        )
        filtering_criteria = {
            "task_names": self.current_task_name,
            "task_types": task_type,
            "host_names": self._create_context.host_name
        }
        profile = filter_profiles(
            filter_creator_profiles,
            filtering_criteria,
            logger=self.log
        )

        allowed_creator_pattern = None
        if profile:
            allowed_creator_labels = {
                label
                for label in profile["creator_labels"]
                if label
            }
            self.log.debug(f"Only allowed `{allowed_creator_labels}` creators")
            allowed_creator_pattern = (
                re.compile("|".join(allowed_creator_labels)))
        return allowed_creator_pattern

    def _is_label_allowed(self, label, allowed_labels_regex):
        """Implement regex support for allowed labels.

        Args:
            label (str): Label of creator - shown in Publisher
            allowed_labels_regex (re.Pattern): compiled regular expression
        """
        if not allowed_labels_regex:
            return True
        return bool(allowed_labels_regex.match(label))

    def _reset_instances(self):
        """Reset create instances."""
        if self._resetting_instances:
            return

        self._resetting_instances = True

        self._create_context.reset_context_data()
        with self._create_context.bulk_instances_collection():
            try:
                self._create_context.reset_instances()
            except CreatorsOperationFailed as exc:
                self._emit_event(
                    "instances.collection.failed",
                    {
                        "title": "Instance collection failed",
                        "failed_info": exc.failed_info
                    }
                )

            try:
                self._create_context.find_convertor_items()
            except ConvertorsOperationFailed as exc:
                self._emit_event(
                    "convertors.find.failed",
                    {
                        "title": "Collection of unsupported product failed",
                        "failed_info": exc.failed_info
                    }
                )

            try:
                self._create_context.execute_autocreators()

            except CreatorsOperationFailed as exc:
                self._emit_event(
                    "instances.create.failed",
                    {
                        "title": "AutoCreation failed",
                        "failed_info": exc.failed_info
                    }
                )

        self._resetting_instances = False

        self._on_create_instance_change()

    def get_thumbnail_paths_for_instances(self, instance_ids):
        thumbnail_paths_by_instance_id = (
            self._create_context.thumbnail_paths_by_instance_id
        )
        return {
            instance_id: thumbnail_paths_by_instance_id.get(instance_id)
            for instance_id in instance_ids
        }

    def set_thumbnail_paths_for_instances(self, thumbnail_path_mapping):
        thumbnail_paths_by_instance_id = (
            self._create_context.thumbnail_paths_by_instance_id
        )
        for instance_id, thumbnail_path in thumbnail_path_mapping.items():
            thumbnail_paths_by_instance_id[instance_id] = thumbnail_path

        self._emit_event(
            "instance.thumbnail.changed",
            {
                "mapping": thumbnail_path_mapping
            }
        )

    def emit_card_message(
        self, message, message_type=CardMessageTypes.standard
    ):
        self._emit_event(
            "show.card.message",
            {
                "message": message,
                "message_type": message_type
            }
        )

    def get_creator_attribute_definitions(self, instances):
        """Collect creator attribute definitions for multuple instances.

        Args:
            instances(List[CreatedInstance]): List of created instances for
                which should be attribute definitions returned.
        """

        # NOTE it would be great if attrdefs would have hash method implemented
        #   so they could be used as keys in dictionary
        output = []
        _attr_defs = {}
        for instance in instances:
            for attr_def in instance.creator_attribute_defs:
                found_idx = None
                for idx, _attr_def in _attr_defs.items():
                    if attr_def == _attr_def:
                        found_idx = idx
                        break

                value = None
                if attr_def.is_value_def:
                    value = instance.creator_attributes[attr_def.key]
                if found_idx is None:
                    idx = len(output)
                    output.append((attr_def, [instance], [value]))
                    _attr_defs[idx] = attr_def
                else:
                    item = output[found_idx]
                    item[1].append(instance)
                    item[2].append(value)
        return output

    def get_publish_attribute_definitions(self, instances, include_context):
        """Collect publish attribute definitions for passed instances.

        Args:
            instances(list<CreatedInstance>): List of created instances for
                which should be attribute definitions returned.
            include_context(bool): Add context specific attribute definitions.
        """

        _tmp_items = []
        if include_context:
            _tmp_items.append(self._create_context)

        for instance in instances:
            _tmp_items.append(instance)

        all_defs_by_plugin_name = {}
        all_plugin_values = {}
        for item in _tmp_items:
            for plugin_name, attr_val in item.publish_attributes.items():
                attr_defs = attr_val.attr_defs
                if not attr_defs:
                    continue

                if plugin_name not in all_defs_by_plugin_name:
                    all_defs_by_plugin_name[plugin_name] = attr_val.attr_defs

                if plugin_name not in all_plugin_values:
                    all_plugin_values[plugin_name] = {}

                plugin_values = all_plugin_values[plugin_name]

                for attr_def in attr_defs:
                    if isinstance(attr_def, UIDef):
                        continue
                    if attr_def.key not in plugin_values:
                        plugin_values[attr_def.key] = []
                    attr_values = plugin_values[attr_def.key]

                    value = attr_val[attr_def.key]
                    attr_values.append((item, value))

        output = []
        for plugin in self._create_context.plugins_with_defs:
            plugin_name = plugin.__name__
            if plugin_name not in all_defs_by_plugin_name:
                continue
            output.append((
                plugin_name,
                all_defs_by_plugin_name[plugin_name],
                all_plugin_values
            ))
        return output

    def get_product_name(
        self,
        creator_identifier,
        variant,
        task_name,
        folder_path,
        instance_id=None
    ):
        """Get product name based on passed data.

        Args:
            creator_identifier (str): Identifier of creator which should be
                responsible for product name creation.
            variant (str): Variant value from user's input.
            task_name (str): Name of task for which is instance created.
            folder_path (str): Folder path for which is instance created.
            instance_id (Union[str, None]): Existing instance id when product
                name is updated.
        """

        creator = self._creators[creator_identifier]

        instance = None
        if instance_id:
            instance = self.instances[instance_id]

        project_name = self.project_name
        folder_item = self._hierarchy_model.get_folder_item_by_path(
            project_name, folder_path
        )
        folder_entity = None
        task_item = None
        task_entity = None
        if folder_item is not None:
            folder_entity = self._hierarchy_model.get_folder_entity(
                project_name, folder_item.entity_id
            )
            task_item = self._hierarchy_model.get_task_item_by_name(
                project_name, folder_item.entity_id, task_name, "controller"
            )

        if task_item is not None:
            task_entity = self._hierarchy_model.get_task_entity(
                project_name, task_item.task_id
            )

        return creator.get_product_name(
            project_name,
            folder_entity,
            task_entity,
            variant,
            instance=instance
        )

    def trigger_convertor_items(self, convertor_identifiers):
        """Trigger legacy item convertors.

        This functionality requires to save and reset CreateContext. The reset
        is needed so Creators can collect converted items.

        Args:
            convertor_identifiers (list[str]): Identifiers of convertor
                plugins.
        """

        success = True
        try:
            self._create_context.run_convertors(convertor_identifiers)

        except ConvertorsOperationFailed as exc:
            success = False
            self._emit_event(
                "convertors.convert.failed",
                {
                    "title": "Conversion failed",
                    "failed_info": exc.failed_info
                }
            )

        if success:
            self.emit_card_message("Conversion finished")
        else:
            self.emit_card_message("Conversion failed", CardMessageTypes.error)

        self.reset()

    def create(
        self, creator_identifier, product_name, instance_data, options
    ):
        """Trigger creation and refresh of instances in UI."""

        success = True
        try:
            self._create_context.create_with_unified_error(
                creator_identifier, product_name, instance_data, options
            )

        except CreatorsOperationFailed as exc:
            success = False
            self._emit_event(
                "instances.create.failed",
                {
                    "title": "Creation failed",
                    "failed_info": exc.failed_info
                }
            )

        self._on_create_instance_change()
        return success

    def save_changes(self, show_message=True):
        """Save changes happened during creation.

        Trigger save of changes using host api. This functionality does not
        validate anything. It is required to do checks before this method is
        called to be able to give user actionable response e.g. check of
        context using 'host_context_has_changed'.

        Args:
            show_message (bool): Show message that changes were
                saved successfully.

        Returns:
            bool: Save of changes was successful.
        """

        if not self._create_context.host_is_valid:
            # TODO remove
            # Fake success save when host is not valid for CreateContext
            #   this is for testing as experimental feature
            return True

        try:
            self._create_context.save_changes()
            if show_message:
                self.emit_card_message("Saved changes..")
            return True

        except CreatorsOperationFailed as exc:
            self._emit_event(
                "instances.save.failed",
                {
                    "title": "Instances save failed",
                    "failed_info": exc.failed_info
                }
            )

        return False

    def remove_instances(self, instance_ids):
        """Remove instances based on instance ids.

        Args:
            instance_ids (List[str]): List of instance ids to remove.
        """

        # QUESTION Expect that instances are really removed? In that case reset
        #    is not required.
        self._remove_instances_from_context(instance_ids)

        self._on_create_instance_change()

    def _remove_instances_from_context(self, instance_ids):
        instances_by_id = self._create_context.instances_by_id
        instances = [
            instances_by_id[instance_id]
            for instance_id in instance_ids
        ]
        try:
            self._create_context.remove_instances(instances)
        except CreatorsOperationFailed as exc:
            self._emit_event(
                "instances.remove.failed",
                {
                    "title": "Instance removement failed",
                    "failed_info": exc.failed_info
                }
            )

    def _on_create_instance_change(self):
        self._emit_event("instances.refresh.finished")

    def get_publish_report(self):
        return self._publish_report.get_report(self._publish_plugins)

    def get_validation_errors(self):
        return self._publish_validation_errors.create_report()

    def _reset_publish(self):
        self._reset_attributes()

        self._publish_up_validation = False
        self._publish_comment_is_set = False

        self._main_thread_iter = self._publish_iterator()
        self._publish_context = pyblish.api.Context()
        # Make sure "comment" is set on publish context
        self._publish_context.data["comment"] = ""
        # Add access to create context during publishing
        # - must not be used for changing CreatedInstances during publishing!
        # QUESTION
        # - pop the key after first collector using it would be safest option?
        self._publish_context.data["create_context"] = self._create_context

        self._publish_plugins_proxy = PublishPluginsProxy(
            self._publish_plugins
        )

        self._publish_report.reset(self._publish_context, self._create_context)
        self._publish_validation_errors.reset(self._publish_plugins_proxy)

        self.publish_max_progress = len(self._publish_plugins)

        self._emit_event("publish.reset.finished")

    def set_comment(self, comment):
        """Set comment from ui to pyblish context.

        This should be called always before publishing is started but should
        happen only once on first publish start thus variable
        '_publish_comment_is_set' is used to keep track about the information.
        """

        if not self._publish_comment_is_set:
            self._publish_context.data["comment"] = comment
            self._publish_comment_is_set = True

    def publish(self):
        """Run publishing.

        Make sure all changes are saved before method is called (Call
        'save_changes' and check output).
        """
        self._btn_clicked = "publish"

        self._publish_up_validation = False
        self._start_publish()

    def validate(self):
        """Run publishing and stop after Validation.

        Make sure all changes are saved before method is called (Call
        'save_changes' and check output).
        """
        self._btn_clicked = "validate"

        if self.publish_has_validated:
            return
        self._publish_up_validation = True
        self._start_publish()

    def ignore_non_blocking_errors(self):
        """ Ignore validation non-blocking errors and continue
        the publishing process.
        """
        self._ignore_non_blocking_errors = True
        self._reset_publish()

        # Logic to resume publishing, similar to ending validation
        if self.publish_is_running:
            self.log.info("Publishing is currently running and will continue.")
        else:
            self.log.info(
                "Resuming publishing after ignoring non-blocking errors.")
            if self._btn_clicked == "validate":
                self.validate()
            elif self._btn_clicked == "publish":
                self.publish()

    def _start_publish(self):
        """Start or continue in publishing."""
        if self.publish_is_running:
            return

        self.publish_is_running = True
        self.publish_has_started = True

        self._emit_event("publish.process.started")

        self._publish_next_process()

    def _stop_publish(self):
        """Stop or pause publishing."""
        self.publish_is_running = False
        self._ignore_non_blocking_errors = False
        self._emit_event("publish.process.stopped")

    def stop_publish(self):
        """Stop publishing process (any reason)."""

        if self.publish_is_running:
            self._stop_publish()

    def run_action(self, plugin_id, action_id):
        # TODO handle result in UI
        plugin = self._publish_plugins_proxy.get_plugin(plugin_id)
        action = self._publish_plugins_proxy.get_action(plugin_id, action_id)

        result = pyblish.plugin.process(
            plugin, self._publish_context, None, action.id
        )
        exception = result.get("error")
        if exception:
            self._emit_event(
                "publish.action.failed",
                {
                    "title": "Action failed",
                    "message": "Action failed.",
                    "traceback": "".join(
                        traceback.format_exception(
                            type(exception),
                            exception,
                            exception.__traceback__
                        )
                    ),
                    "label": action.__name__,
                    "identifier": action.id
                }
            )

        self._publish_report.add_action_result(action, result)

        self.emit_card_message("Action finished.")

    #TODO: Test the _is_interactive method in both interactive and batch modes.
    @property
    def _is_interactive(self):
        """Check for Interactive Mode

        Determine if the script is running in interactive mode
        (as opposed to batch mode) based on the host application.
        """

        host = os.getenv('AYON_HOST_NAME')

        if host is None:
            raise EnvironmentError(
                "Host environment variable 'AYON_HOST_NAME' is not set")
        elif host == 'maya':
            import maya.cmds
            return not maya.cmds.about(query=True, batch=True)
        elif host == 'houdini':
            import hou
            return hou.isUIAvailable()
        elif host == 'blender':
            import bpy
            return not bpy.app.background
        else:
            raise ValueError(f"Unknown host environment: {host}")

    def _publish_next_process(self):
        # Validations of progress before using iterator
        # - same conditions may be inside iterator but they may be used
        #   only in specific cases (e.g. when it happens for a first time)

        # There are validation errors and validation is passed
        # - can't do any progree

        # TODO: Test the _is_interactive method in both
        #  interactive and batch modes.
        # Determine the conditions to stop the publish process depending
        # on whether the mode is interactive or non-interactive
        if self._is_interactive:
            # In interactive mode, stop for any validation
            # errors if validation has passed
            stop_publish_conditions = (
                    self.publish_has_validated
                    and self.publish_has_validation_errors
            )
        else:
            # In batch mode, stop for blocking validation
            # errors only if validation has passed
            stop_publish_conditions = (
                    self.publish_has_validated
                    and self.publish_has_validation_blocking_errors
            )
        if stop_publish_conditions:
            item = MainThreadItem(self.stop_publish)

        # Any unexpected error happened
        # - everything should stop
        elif self.publish_has_crashed:
            item = MainThreadItem(self.stop_publish)

        # Everything is ok so try to get new processing item
        else:
            item = next(self._main_thread_iter)

        self._process_main_thread_item(item)

    def _process_main_thread_item(self, item):
        item()

    def _is_publish_plugin_active(self, plugin):
        """Decide if publish plugin is active.

        This is hack because 'active' is mis-used in mixin
        'OptionalPyblishPluginMixin' where 'active' is used for default value
        of optional plugins. Because of that is 'active' state of plugin
        which inherit from 'OptionalPyblishPluginMixin' ignored. That affects
        headless publishing inside host, potentially remote publishing.

        We have to change that to match pyblish base, but we can do that
        only when all hosts use Publisher because the change requires
        change of settings schemas.

        Args:
            plugin (pyblish.Plugin): Plugin which should be checked if is
                active.

        Returns:
            bool: Is plugin active.
        """

        if plugin.active:
            return True

        if not plugin.optional:
            return False

        if OptionalPyblishPluginMixin in inspect.getmro(plugin):
            return True
        return False

    def _publish_iterator(self):
        """Main logic center of publishing.

        Iterator returns `MainThreadItem` objects with callbacks that should be
        processed in main thread (threaded in future?). Cares about changing
        states of currently processed publish plugin and instance. Also
        change state of processed orders like validation order has passed etc.

        Also stops publishing, if should stop on validation.
        """

        for idx, plugin in enumerate(self._publish_plugins):
            self._publish_progress = idx

            # Check if plugin is over validation order
            if not self.publish_has_validated:
                self.publish_has_validated = (
                    plugin.order >= self._validation_order
                )

            # Stop if plugin is over validation order and process
            #   should process up to validation.
            if self._publish_up_validation and self.publish_has_validated:
                yield MainThreadItem(self.stop_publish)

            # TODO: Test the _is_interactive method in both
            #  interactive and batch modes.
            # Stop if validation is over and validation errors happened
            if self.publish_has_validated:
                if self._is_interactive:
                    # In interactive mode, stop publish for
                    # any validation errors
                    if self.publish_has_validation_errors:
                        yield MainThreadItem(self.stop_publish)
                else:
                    # In batch mode, stop publish for
                    # blocking validation errors only
                    if self.publish_has_validation_blocking_errors:
                        yield MainThreadItem(self.stop_publish)

            # Add plugin to publish report
            self._publish_report.add_plugin_iter(
                plugin, self._publish_context)

            # WARNING This is hack fix for optional plugins
            if not self._is_publish_plugin_active(plugin):
                self._publish_report.set_plugin_skipped()
                continue

            # Trigger callback that new plugin is going to be processed
            plugin_label = plugin.__name__
            if hasattr(plugin, "label") and plugin.label:
                plugin_label = plugin.label
            self._emit_event(
                "publish.process.plugin.changed",
                {"plugin_label": plugin_label}
            )

            # Plugin is instance plugin
            if plugin.__instanceEnabled__:
                instances = pyblish.logic.instances_by_plugin(
                    self._publish_context, plugin
                )
                if not instances:
                    self._publish_report.set_plugin_skipped()
                    continue

                for instance in instances:
                    if instance.data.get("publish") is False:
                        continue

                    instance_label = (
                        instance.data.get("label")
                        or instance.data["name"]
                    )
                    self._emit_event(
                        "publish.process.instance.changed",
                        {"instance_label": instance_label}
                    )

                    yield MainThreadItem(
                        self._process_and_continue, plugin, instance
                    )
            else:
                families = collect_families_from_instances(
                    self._publish_context, only_active=True
                )
                plugins = pyblish.logic.plugins_by_families(
                    [plugin], families
                )
                if plugins:
                    instance_label = (
                        self._publish_context.data.get("label")
                        or self._publish_context.data.get("name")
                        or "Context"
                    )
                    self._emit_event(
                        "publish.process.instance.changed",
                        {"instance_label": instance_label}
                    )
                    yield MainThreadItem(
                        self._process_and_continue, plugin, None
                    )
                else:
                    self._publish_report.set_plugin_skipped()

        # Cleanup of publishing process
        self.publish_has_finished = True
        self.publish_progress = self.publish_max_progress
        yield MainThreadItem(self.stop_publish)

    @staticmethod
    def _set_error_title_if_missing(plugin, error):
        """Ensure that the validation report item has a title."""
        if not error.title:
            error.title = getattr(plugin, 'label',
                                  plugin.__name__)

    def _add_validation_error(self, result: dict) -> None:
        self._set_error_title_if_missing(result["plugin"], result["error"])
        if not result["is_blocking"] and self._ignore_non_blocking_errors:
            self.log.info(
                f"Ignoring non-blocking error: {result['error'].title}")
            return

        self.publish_has_validation_errors = True

        if result["is_blocking"]:
            self.publish_has_validation_blocking_errors = True

        self._publish_validation_errors.add_error(
            result["plugin"],
            result["error"],
            result["instance"]
        )

    def _process_and_continue(self, plugin, instance):
        result = pyblish.plugin.process(
            plugin, self._publish_context, instance
        )

        exception = result.get("error")

        self._publish_plugins_proxy.set_plugin_actions(plugin.id, exception)

        if exception:
            has_validation_error = False
            if (
                isinstance(exception, PublishValidationError)
                and not self.publish_has_validated
            ):
                has_validation_error = True
                result["is_blocking"] = exception.is_blocking
                self._add_validation_error(result)

            else:
                if isinstance(exception, KnownPublishError):
                    msg = str(exception)
                else:
                    msg = (
                        "Something went wrong. Send report"
                        " to your supervisor or Ynput team."
                    )
                self.publish_error_msg = msg
                self.publish_has_crashed = True

            result["is_validation_error"] = has_validation_error

        self._publish_report.add_result(result)
        self._publish_report.set_plugin_action_items(
            self._publish_plugins_proxy.get_plugin_action_items(plugin.id))

        self._publish_next_process()


def collect_families_from_instances(instances, only_active=False):
    """Collect all families for passed publish instances.

    Args:
        instances(list<pyblish.api.Instance>): List of publish instances from
            which are families collected.
        only_active(bool): Return families only for active instances.

    Returns:
        list[str]: Families available on instances.
    """

    all_families = set()
    for instance in instances:
        if only_active:
            if instance.data.get("publish") is False:
                continue
        family = instance.data.get("family")
        if family:
            all_families.add(family)

        families = instance.data.get("families") or tuple()
        for family in families:
            all_families.add(family)

    return list(all_families)
