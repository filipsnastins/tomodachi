import asyncio
import inspect
import os
import re
import sys
import types
import uuid
from types import ModuleType, TracebackType
from typing import Any, Dict, Optional, Set, Type, cast

import tomodachi
from tomodachi import CLASS_ATTRIBUTE, logging
from tomodachi.helpers.dict import merge_dicts
from tomodachi.helpers.execution_context import set_service, unset_service
from tomodachi.invoker import FUNCTION_ATTRIBUTE, INVOKER_TASK_START_KEYWORD, START_ATTRIBUTE


class ServiceContainer(object):
    def __init__(self, module_import: ModuleType, configuration: Optional[Dict] = None) -> None:
        self.module_import = module_import

        self.file_path = module_import.__file__
        self.module_name = (
            module_import.__name__.rsplit("/", 1)[1] if "/" in module_import.__name__ else module_import.__name__
        ).rsplit(".", 1)[-1]
        self.configuration = configuration
        self.logger = logging.getLogger("tomodachi")
        # self.logger = self.logger.bind(file_path=self.file_path)

        self._close_waiter: Optional[asyncio.Future] = None
        self.started_waiter: Optional[asyncio.Future] = None

        def catch_uncaught_exceptions(
            type_: Type[BaseException], value: BaseException, traceback: Optional[TracebackType]
        ) -> Any:
            raise value

        sys.excepthook = catch_uncaught_exceptions

    def stop_service(self) -> None:
        if not self._close_waiter:
            self._close_waiter = asyncio.Future()

        if not self._close_waiter.done():
            self._close_waiter.set_result(None)

    def setup_configuration(self, instance: Any) -> None:
        if not self.configuration:
            return
        for k, v in self.configuration.items():
            instance_value = getattr(instance, k, None)
            if not instance_value:
                setattr(instance, k, v)

            if isinstance(instance_value, list) and isinstance(v, list):
                setattr(instance, k, instance_value + v)
            elif isinstance(instance_value, dict) and isinstance(v, dict):
                setattr(instance, k, merge_dicts(instance_value, v))
            else:
                setattr(instance, k, v)

    async def wait_stopped(self) -> None:
        if not self._close_waiter:
            self._close_waiter = asyncio.Future()

        await self._close_waiter

    async def run_until_complete(self) -> None:
        services_started: Set = set()
        invoker_tasks: Set = set()
        setup_coros: Set = set()
        teardown_coros: Set = set()
        interrupt_coros: Set = set()
        initialized_coros: Set = set()
        registered_services: Set = set()

        if not self.started_waiter:
            self.started_waiter = asyncio.Future()

        tomodachi.get_contextvar("service.logger").set("service")

        def logging_context_wrapper(coro: Any, service_name_: str, **logger_context: Any) -> Any:
            async def _wrapper(*args: Any, **kwargs: Any) -> Any:
                if not coro:
                    return

                logger = logging.get_logger("service").bind(service_name=service_name_)
                if logger_context:
                    logger = logging.get_logger().new(**logger_context)
                logging.bind_logger(logger)
                name = logger._context.get("logger", "service")
                if name == "service" or name.startswith("service."):
                    tomodachi.get_contextvar("service.logger").set(name)

                return await coro(*args, **kwargs)

            return _wrapper

        for _, cls in inspect.getmembers(self.module_import):
            if inspect.isclass(cls):
                if not getattr(cls, CLASS_ATTRIBUTE, False):
                    continue

                instance = cls()
                if not getattr(instance, "context", None):
                    setattr(
                        instance,
                        "context",
                        {
                            i: getattr(instance, i)
                            for i in dir(instance)
                            if not callable(i)
                            and not i.startswith("__")
                            and not isinstance(getattr(instance, i), types.MethodType)
                        },
                    )

                getattr(instance, "context", {})["_service_file_path"] = self.file_path

                self.setup_configuration(instance)

                context_options = getattr(instance, "context", {}).get("options", {})
                if context_options:
                    for key in list(context_options.keys()):
                        if "." in key:
                            key_split = key.split(".")
                            op_lvl = context_options
                            for i, k_lvl in enumerate(key_split):
                                if i + 1 == len(key_split):
                                    if k_lvl in op_lvl and op_lvl[k_lvl] != context_options[key]:
                                        raise Exception(
                                            'Missmatching options for \'{}\': ({}) "{}" and ({}) "{}" differs'.format(
                                                key,
                                                type(context_options[key]).__name__,
                                                context_options[key],
                                                type(op_lvl[k_lvl]).__name__,
                                                op_lvl[k_lvl],
                                            )
                                        )
                                    op_lvl[k_lvl] = context_options[key]
                                    continue
                                if k_lvl not in op_lvl:
                                    op_lvl[k_lvl] = {}
                                op_lvl = op_lvl.get(k_lvl)

                if not getattr(instance, "uuid", None):
                    instance.uuid = str(uuid.uuid4())

                service_name = getattr(instance, "name", getattr(cls, "name", None))

                if not service_name:
                    service_name = ServiceContainer.assign_service_name(instance)
                    if not service_name:
                        continue

                set_service(service_name, instance)

                log_level = getattr(instance, "log_level", None) or getattr(cls, "log_level", None) or "INFO"

                def invoker_function_sorter(m: str) -> int:
                    for i, line in enumerate(inspect.getsourcelines(self.module_import)[0]):
                        if re.match(r"^\s*(async)?\s+def\s+{}\s*([(].*$)?$".format(m), line):
                            return i
                    return -1

                invoker_functions = []
                for name, fn in inspect.getmembers(cls):
                    if inspect.isfunction(fn) and getattr(fn, FUNCTION_ATTRIBUTE, None):
                        setattr(fn, START_ATTRIBUTE, True)  # deprecated
                        invoker_functions.append(name)
                invoker_functions.sort(key=invoker_function_sorter)
                if invoker_functions:
                    invoker_tasks = invoker_tasks | set(
                        [
                            (
                                service_name,
                                asyncio.ensure_future(
                                    logging_context_wrapper(
                                        getattr(instance, name),
                                        service_name,
                                        logger="tomodachi.setup",
                                        wrapped_handler=name,
                                    )(**{INVOKER_TASK_START_KEYWORD: True})
                                ),
                            )
                            for name in invoker_functions
                        ]
                    )
                    services_started.add((service_name, instance, log_level))

                try:
                    setup_coros.add(
                        logging_context_wrapper(
                            getattr(instance, "_start_service"),
                            service_name,
                            logger="service.handler",
                            # service_handler="_start_service",
                            # operation="lifecycle.initialize",
                            # triggered="start",
                            handler="_start_service",
                            handler_type="tomodachi.lifecycle",
                            operation="setup",
                        )
                    )
                    services_started.add((service_name, instance, log_level))
                except AttributeError:
                    pass

                if getattr(instance, "_started_service", None):
                    services_started.add((service_name, instance, log_level))

        if services_started:
            try:
                for name, instance, log_level in services_started:
                    # self.logger.info('Initializing service "{}" [id: {}]'.format(name, instance.uuid))
                    self.logger.info(
                        "initializing service",
                        state="initializing",
                        event_="lifecycle.setup",
                        service=name,
                    )

                if setup_coros:
                    results = await asyncio.wait([asyncio.ensure_future(func()) for func in setup_coros if func])
                    exceptions = [v.exception() for v in [value for value in results if value][0] if v.exception()]
                    if exceptions:
                        for exception in exceptions:
                            try:
                                raise cast(Exception, exception)
                            except Exception as e:
                                logging.getLogger("exception").exception("Uncaught exception: {}".format(str(e)))

                        for name, instance, log_level in services_started:
                            self.logger.warning(
                                "failed to start service", state="aborting", event_="lifecycle.abort", service=name
                            )

                        if invoker_tasks:
                            await asyncio.gather(*set([future for _, future in invoker_tasks]))
                            invoker_tasks = set()

                        tomodachi.SERVICE_EXIT_CODE = 1
                        self.stop_service()

                if invoker_tasks:
                    await asyncio.gather(*set([future for _, future in invoker_tasks]))
                    invoker_coros = set(
                        [
                            logging_context_wrapper(
                                future.result(),
                                name,
                                logger="tomodachi.invoker",
                                invoker_module=future.result().__module__,
                                invoker_function=future.result().__qualname__.split(".<locals>", 1)[0],
                            )
                            for name, future in invoker_tasks
                            if future and future.result()
                        ]
                    )
                    results = await asyncio.wait([asyncio.ensure_future(func()) for func in invoker_coros if func])
                    # print(name, future)
                    # task_results = await asyncio.wait(
                    #     [
                    #         asyncio.ensure_future(func())
                    #         for func in (await asyncio.gather(*invoker_tasks))
                    #         if print(func.__qualname__ if func else func) or func
                    #     ]
                    # )
                    exceptions = [v.exception() for v in [value for value in results if value][0] if v.exception()]
                    if exceptions:
                        for exception in exceptions:
                            try:
                                raise cast(Exception, exception)
                            except Exception as e:
                                logging.getLogger("exception").exception("Uncaught exception: {}".format(str(e)))

                        for name, instance, log_level in services_started:
                            self.logger.warning(
                                "failed to start service", state="aborting", event_="lifecycle.abort", service=name
                            )

                        tomodachi.SERVICE_EXIT_CODE = 1
                        self.stop_service()

                if (
                    self.started_waiter
                    and not self.started_waiter.done()
                    and (not self._close_waiter or not self._close_waiter.done())
                ):
                    for name, instance, log_level in services_started:
                        for registry in getattr(instance, "discovery", []):
                            registered_services.add((name, instance))
                            if getattr(registry, "_register_service", None):
                                await asyncio.create_task(
                                    logging_context_wrapper(
                                        registry._register_service,
                                        name,
                                        logger="tomodachi.discovery",
                                        registry=registry.name
                                        if hasattr(registry, "name")
                                        else (
                                            registry.__name__
                                            if hasattr(registry, "__name__")
                                            else (
                                                type(registry).__name__
                                                if hasattr(type(registry), "__name__")
                                                else str(type(registry))
                                            )
                                        ),
                                        operation="discovery.register",
                                    )(instance)
                                )

                        if getattr(instance, "_started_service", None):
                            initialized_coros.add(
                                logging_context_wrapper(
                                    getattr(instance, "_started_service", None),
                                    name,
                                    logger="service.handler",
                                    # operation="lifecycle.ready",
                                    # handler_function="_started_service",
                                    handler="_started_service",
                                    handler_type="tomodachi.lifecycle",
                                    operation="initialized",
                                )
                            )

                    # self.logger.info('Started service "{}" [id: {}]'.format(name, instance.uuid))
                    self.logger.info(
                        "service handlers initialized",
                        state="initialized",
                        event_="lifecycle.initialized",
                        service=name,
                    )
            except Exception as e:
                # self.logger.warning("Failed to start service")
                logging.getLogger("exception").exception("Uncaught exception: {}".format(str(e)))

                for name, instance, log_level in services_started:
                    self.logger.warning(
                        "failed to start service", state="aborting", event_="lifecycle.abort", service=name
                    )

                tomodachi.SERVICE_EXIT_CODE = 1
                initialized_coros = set()
                self.stop_service()

            if initialized_coros and any(initialized_coros):
                results = await asyncio.wait([asyncio.ensure_future(func()) for func in initialized_coros if func])
                exceptions = [v.exception() for v in [value for value in results if value][0] if v.exception()]
                if exceptions:
                    for exception in exceptions:
                        try:
                            raise cast(Exception, exception)
                        except Exception as e:
                            logging.getLogger("exception").exception("Uncaught exception: {}".format(str(e)))

                    for name, instance, log_level in services_started:
                        self.logger.warning(
                            "failed to start service", state="aborting", event_="lifecycle.abort", service=name
                        )

                    tomodachi.SERVICE_EXIT_CODE = 1
                    self.stop_service()

        else:
            # self.logger.warning("No transports defined in service file")
            self.logger.warning("no transport handlers defined", module=self.module_name, file_path=self.file_path)
            tomodachi.SERVICE_EXIT_CODE = 1
            self.stop_service()

        self.services_started = services_started
        if self.started_waiter and not self.started_waiter.done():
            self.started_waiter.set_result(services_started)

            if not self._close_waiter or not self._close_waiter.done():
                for name, instance, log_level in services_started:
                    self.logger.info(
                        "service started successfully", state="ready", event_="lifecycle.ready", service=name
                    )

        await self.wait_stopped()

        for name, instance, log_level in services_started:
            if getattr(instance, "_stopping_service", None):
                interrupt_coros.add(
                    logging_context_wrapper(
                        getattr(instance, "_stopping_service", None),
                        name,
                        logger="service.handler",
                        # operation="lifecycle.stop",
                        # handler_function="_stopping_service",
                        handler="_stopping_service",
                        handler_type="tomodachi.lifecycle",
                        operation="interrupt",
                    )
                )

            if getattr(instance, "_stop_service", None):
                teardown_coros.add(
                    logging_context_wrapper(
                        getattr(instance, "_stop_service", None),
                        name,
                        logger="service.handler",
                        # operation="lifecycle.stop",
                        # handler_function="_stop_service",
                        handler="_stop_service",
                        handler_type="tomodachi.lifecycle",
                        operation="teardown",
                        # lifecycle="stopping",
                    )
                )

        for name, instance, log_level in services_started:
            # self.logger.info('Stopping service "{}" [id: {}]'.format(name, instance.uuid))
            self.logger.info("lifecycle interrupt notice", state="stopping", event_="lifecycle.interrupt", service=name)

        interrupt_futures = []
        if interrupt_coros and any(interrupt_coros):
            interrupt_futures = [asyncio.ensure_future(func()) for func in interrupt_coros if func]
            await asyncio.sleep(0.01)

        for name, instance, log_level in services_started:
            # self.logger.info('Stopping service "{}" [id: {}]'.format(name, instance.uuid))
            self.logger.info("stopping service", state="stopping", event_="lifecycle.teardown", service=name)

        for name, instance in registered_services:
            for registry in getattr(instance, "discovery", []):
                if getattr(registry, "_deregister_service", None):
                    await asyncio.create_task(
                        logging_context_wrapper(
                            registry._deregister_service,
                            name,
                            logger="tomodachi.discovery",
                            registry=registry.name
                            if hasattr(registry, "name")
                            else (
                                registry.__name__
                                if hasattr(registry, "__name__")
                                else (
                                    type(registry).__name__
                                    if hasattr(type(registry), "__name__")
                                    else str(type(registry))
                                )
                            ),
                            operation="discovery.deregister",
                            # function="{}.{}.{}".format(
                            #     registry.__module__,
                            #     registry.__name__
                            #     if hasattr(registry, "__name__")
                            #     else (
                            #         type(registry).__name__
                            #         if hasattr(type(registry), "__name__")
                            #         else str(type(registry))
                            #     ),
                            #     "_deregister_service",
                            # ),
                            # function=registry._deregister_service.__name__,
                        )(instance)
                    )

        teardown_futures = []
        if teardown_coros and any(teardown_coros):
            teardown_futures = [asyncio.ensure_future(func()) for func in teardown_coros if func]

        if teardown_futures or interrupt_futures:
            results = await asyncio.wait(teardown_futures + interrupt_futures)
            exceptions = [v.exception() for v in [value for value in results if value][0] if v.exception()]
            if exceptions:
                for exception in exceptions:
                    try:
                        raise cast(Exception, exception)
                    except Exception as e:
                        logging.getLogger("exception").exception("Uncaught exception: {}".format(str(e)))

        for name, instance, log_level in services_started:
            # self.logger.info('Stopped service "{}" [id: {}]'.format(name, instance.uuid))
            self.logger.info("terminated service", state="terminated", service=name)

        # Debug output if TOMODACHI_DEBUG env is set. Shows still running tasks on service termination.
        if os.environ.get("TOMODACHI_DEBUG") and os.environ.get("TOMODACHI_DEBUG") != "0":
            try:
                tasks = [task for task in asyncio.all_tasks()]
                for task in tasks:
                    try:
                        co_filename = task.get_coro().cr_code.co_filename if hasattr(task, "get_coro") else task._coro.cr_code.co_filename  # type: ignore
                        co_name = task.get_coro().cr_code.co_name if hasattr(task, "get_coro") else task._coro.cr_code.co_name  # type: ignore

                        if "/tomodachi/watcher.py" in co_filename and co_name == "_watch_loop":
                            continue
                        if "/tomodachi/container.py" in co_filename and co_name == "run_until_complete":
                            continue
                        if "/asyncio/tasks.py" in co_filename and co_name == "wait":
                            continue

                        # self.logger.warning(
                        #     "** Task '{}' from '{}' has not finished execution or has not been awaited".format(
                        #         co_name, co_filename
                        #     )
                        # )
                        self.logger.warning(
                            "task has not been awaited", task_function_name=co_name, task_filename=co_filename
                        )

                    except Exception:
                        pass
            except Exception:
                pass

    @classmethod
    def assign_service_name(cls, instance: Any) -> str:
        new_service_name = ""
        if instance.__class__.__module__ and instance.__class__.__module__ not in (
            "service.app",
            "service.service",
            "services.app",
            "services.service",
            "src.service",
            "src.app",
            "code.service",
            "code.app",
            "app.service",
            "app.app",
            "apps.service",
            "apps.app",
            "example.service",
            "example.app",
            "examples.service",
            "examples.app",
            "test.service",
            "test.app",
            "tests.service",
            "tests.app",
        ):
            new_service_name = (
                "{}-".format(
                    re.sub(
                        r"^.*[.]([a-zA-Z0-9_]+)[.]([a-zA-Z0-9_]+)$",
                        r"\1-\2",
                        str(instance.__class__.__module__),
                    )
                )
                .replace("_", "-")
                .replace(".", "-")
            )
        class_name = instance.__class__.__name__
        for i, c in enumerate(class_name.lower()):
            if i and c != class_name[i]:
                new_service_name += "-"
            if c == "_":
                c = "-"
            new_service_name += c

        if new_service_name in ("app", "service"):
            new_service_name = "service"

        if not tomodachi.get_service(new_service_name) and not tomodachi.get_service(
            "{}-0001".format(new_service_name)
        ):
            service_name = new_service_name
        else:
            if tomodachi.get_service(new_service_name) and not tomodachi.get_service(
                "{}-0001".format(new_service_name)
            ):
                other_service = tomodachi.get_service(new_service_name)
                setattr(other_service, "name", "{}-0001".format(new_service_name))
                setattr(other_service.__class__, "name", other_service.name)
                unset_service(new_service_name)
                set_service(other_service.name, other_service)

            incr = 1
            while True:
                test_service_name = "{}-{:04d}".format(new_service_name, incr)
                if tomodachi.get_service(test_service_name):
                    incr += 1
                    continue
                service_name = test_service_name
                break

        setattr(instance, "name", service_name)
        setattr(instance.__class__, "name", service_name)

        return service_name
