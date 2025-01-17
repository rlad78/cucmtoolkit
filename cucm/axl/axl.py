from typing import Callable, TypeVar, Union
from cucm.axl.validation import (
    validate_ucm_server,
    validate_axl_auth,
    get_ucm_version,
)
from cucm.axl.exceptions import *
from cucm.axl.wsdl import (
    AXLElement,
    get_return_tags,
    fix_return_tags,
    get_tree,
    print_element_layout,
    print_required_element_layout,
    print_return_tags_layout,
    validate_arguments,
)
from cucm.utils import print_signature, Empty
import cucm.axl.configs as cfg
import re
import urllib3
from requests import Session
from requests.auth import HTTPBasicAuth
from zeep import Client, Settings
from zeep.transports import Transport
from zeep.cache import SqliteCache
from zeep.exceptions import Fault
from zeep.helpers import serialize_object
from zeep.xsd import Nil
from functools import wraps
from copy import deepcopy
import inspect
from termcolor import colored
from time import sleep
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

########################
# ----- DECORATORS -----
########################

TCallable = TypeVar("TCallable", bound=Callable)


def serialize(func: TCallable) -> TCallable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        r_value = func(*args, **kwargs)
        if cfg.DISABLE_SERIALIZER:
            return r_value

        if r_value is None:
            return dict()
        elif issubclass(type(r_value), Fault):
            raise AXLFault(r_value)
        elif (
            "return_tags" not in kwargs
            and (
                tags_param := inspect.signature(func).parameters.get(
                    "return_tags", None
                )
            )
            is not None
        ):
            r_dict = serialize_object(r_value, dict)
            return _tag_serialize_filter(tags_param.default, r_dict)
        elif "return_tags" in kwargs:
            r_dict = serialize_object(r_value, dict)
            return _tag_serialize_filter(kwargs["return_tags"], r_dict)
        else:
            return serialize_object(r_value, dict)

    return wrapper


def serialize_list(func: TCallable) -> TCallable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        r_value = func(*args, **kwargs)
        if cfg.DISABLE_SERIALIZER:
            return r_value

        if type(r_value) != list:
            return r_value
        elif (
            "return_tags" not in kwargs
            and (
                tags_param := inspect.signature(func).parameters.get(
                    "return_tags", None
                )
            )
            is not None
        ):
            return [
                _tag_serialize_filter(
                    tags_param.default, serialize_object(element, dict)
                )
                for element in r_value
            ]
        elif "return_tags" in kwargs:
            return [
                _tag_serialize_filter(
                    kwargs["return_tags"], serialize_object(element, dict)
                )
                for element in r_value
            ]

    return wrapper


def check_tags(element_name: str):
    def check_tags_decorator(func: TCallable) -> TCallable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if cfg.DISABLE_CHECK_TAGS:
                return func(*args, **kwargs)

            # if type(args[0]) != Axl:
            if not issubclass(type(args[0]), Axl):
                raise DumbProgrammerException(
                    f"Forgot to include self in {func.__name__}!!!!"
                )
            elif (
                tags_param := inspect.signature(func).parameters.get(
                    "return_tags", None
                )
            ) is None:
                raise DumbProgrammerException(
                    f"No 'return_tags' param on {func.__name__}()"
                )
            elif tags_param.kind != tags_param.KEYWORD_ONLY:
                raise DumbProgrammerException(
                    f"Forgot to add '*' before return_tags on {func.__name__}()"
                )
            elif not element_name:
                raise DumbProgrammerException(
                    f"Forgot to provide element_name in check_tags decorator on {func.__name__}!!!"
                )
            elif "return_tags" not in kwargs:
                # tags are default
                if len(tags_param.default) == 0:
                    kwargs["return_tags"] = fix_return_tags(
                        z_client=args[0].zeep,
                        element_name=element_name,
                        tags=get_return_tags(args[0].zeep, element_name),
                    )
                return func(*args, **kwargs)
            elif type(kwargs["return_tags"]) == list:
                # supply all legal tags if an empty list is provided
                if len(kwargs["return_tags"]) == 0:
                    kwargs["return_tags"] = fix_return_tags(
                        z_client=args[0].zeep,
                        element_name=element_name,
                        tags=get_return_tags(args[0].zeep, element_name),
                    )
                else:
                    kwargs["return_tags"] = fix_return_tags(
                        z_client=args[0].zeep,
                        element_name=element_name,
                        tags=kwargs["return_tags"],
                    )
                return func(*args, **kwargs)

        wrapper.element_name = element_name
        wrapper.check = "tags"
        return wrapper

    return check_tags_decorator


def operation_tag(element_name: str):
    def operation_tag_decorator(func: TCallable) -> TCallable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper.element_name = element_name
        return wrapper

    return operation_tag_decorator


def check_arguments(element_name: str, child=None):
    def check_argument_deorator(func: TCallable) -> TCallable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if cfg.DISABLE_CHECK_ARGS:
                return func(*args, **kwargs)

            # get non-default kwargs
            default_kwargs = list(inspect.signature(func).parameters)
            user_kwargs = {k: v for k, v in kwargs.items() if k not in default_kwargs}
            validate_arguments(args[0].zeep, element_name, child=child, **user_kwargs)
            return func(*args, **kwargs)

        wrapper.element_name = element_name
        wrapper.check = "args"
        return wrapper

    return check_argument_deorator


###################
# ----- CLASS -----
###################


class Axl(object):
    def __init__(
        self,
        username: str,
        password: str,
        cucm: str,
        port="8443",
        version="",
        verbose=False,
    ):
        """Main object for interfacing with the AXL API

        Parameters
        ----------
        username : str
            Admin with AXL privileges
        password : str
            Password for admin user
        cucm : str
            Base URL where a UCM server is located
        port : str, optional
            The port at which UCM services can be accessed, by default "8443"
        version : str, optional
            Only required if getting UDS exceptions, by default "". Use a two-digit version, like "11.5" or "14.0"

        Raises
        ------
        UCMVersionInvalid
            if an invalid UCM version is provided
        UCMException
            if an issue regarding UCM is found
        AXLException
            if an issue regarding the AXL API is found
        """
        if verbose:
            print(f"Attempting to verify {cucm} is a valid UCM server...")
        try:
            ucm_validation = validate_ucm_server(cucm, port)
        except (
            URLInvalidError,
            UCMInvalidError,
            UCMConnectionFailure,
            UCMNotFoundError,
        ) as err:
            raise err  # ? not sure if we still need this...
        if not ucm_validation:
            raise UCMException(
                f"Could not connect to {cucm}, please check your server."
            )

        if version != "":
            cucm_version = version
        else:
            cucm_version = get_ucm_version(cucm, port)
            if verbose:
                print(f"Got UCM version number: {cucm_version}")

        wsdl_path = cfg.ROOT_DIR / "schema" / cucm_version / "AXLAPI.wsdl"
        if not wsdl_path.parent.is_dir():
            raise UCMVersionInvalid(cucm_version)
        wsdl = str(wsdl_path)

        session = Session()
        session.verify = False
        session.auth = HTTPBasicAuth(username, password)
        settings = Settings(
            strict=False, xml_huge_tree=True, xsd_ignore_sequence_order=True
        )
        transport = Transport(session=session, timeout=10, cache=SqliteCache())
        axl_client = Client(wsdl, settings=settings, transport=transport)

        self.username = username
        self.password = password
        self.zeep = axl_client
        # self.schema = XMLSchema(str(wsdl_path.parent / "AXLSoap.xsd"))
        self.wsdl = wsdl
        self.cucm = cucm
        self.cucm_port = port
        self.cucm_version = cucm_version

        if verbose:
            print("Validating AXL connection with given credentials...")
        try:
            axl_validation = validate_axl_auth(cucm, username, password, port)
        except (AXLInvalidCredentials, AXLConnectionFailure, AXLNotFoundError) as err:
            raise err  # ? again, do we really need this?
        if not axl_validation:
            raise AXLException()

        self.UUID_PATTERN = re.compile(
            r"^[\da-f]{8}-([\da-f]{4}-){3}[\da-f]{12}$", re.IGNORECASE
        )

        if verbose:
            print(f"Connecting to AXL service at https://{cucm}:{port}/axl/ ...")
        self.client = axl_client.create_service(
            "{http://www.cisco.com/AXLAPIService/}AXLAPIBinding",
            f"https://{cucm}:{port}/axl/",
        )
        if verbose:
            print("Connection to AXL service established!\n")

    # *******************************
    # ----- [TEMPLATE FETCHING] -----
    # *******************************

    def __extract_template(self, element_name: str, template: dict, child="") -> dict:
        """Removes all unnecessary values from a device/line/etc template, like None and "" values. Keeps any values that are required by the given element_name, regardless of what the values are.

        Args:
            element_name (str): The 'get' element used to get the template
            template (dict): UCM template data
            child (str, optional): Used as a key for the template dict. Use "" to ignore. Defaults to "".

        Returns:
            dict: Template data removed of unnecessary values
        """

        def is_removable(branch: dict) -> bool:
            for value in branch.values():
                if type(value) == dict:
                    if is_removable(value) == False:
                        return False
                elif type(value) == list:
                    if not all([type(v) == dict for v in value]):
                        return False
                    elif not all([is_removable(v) for v in value]):
                        return False
                elif value not in (None, -1, ""):
                    return False
            else:
                return True

        def tree_match(root: AXLElement, template: dict) -> dict:
            result = {}
            for name, value in template.items():
                if (node := root.get(name, None)) is not None:
                    if node.children:
                        if type(value) == dict:
                            result_dict = tree_match(node, value)
                            if (
                                not is_removable(result_dict)
                                or node._parent_chain_required()
                            ):
                                result[name] = result_dict
                        elif type(value) == list:
                            result_list = [tree_match(node, t) for t in value]
                            if all([type(r) == dict for r in result_list]):
                                result_list = [
                                    r for r in result_list if not is_removable(r)
                                ]
                            if result_list:
                                result[name] = result_list
                    elif value is None and node._parent_chain_required():
                        result[name] = Nil
                    elif value not in (None, -1, "") or node._parent_chain_required():
                        # else:
                        result[name] = value
            return result

        tree: AXLElement = get_tree(self.zeep, element_name)
        if child:
            tree = tree.get(child, None)
            if tree is None:
                raise DumbProgrammerException(
                    f"{tree.name} does not have a child named '{child}'"
                )

        result_data = tree_match(tree, template)
        for name, value in deepcopy(result_data).items():
            if tree.get(name)._parent_chain_required():
                # continue
                if value is None:
                    result_data[name] = Nil
            elif value in (None, -1, ""):
                result_data.pop(name)
            elif type(value) == dict and is_removable(value):
                result_data.pop(name)

        return result_data

    def _from_phone_template(self, template_name: str, **kwargs) -> dict:
        """Generates template data from a given UCM phone template. The template data can be used as a base to insert new phones.

        Args:
            template_name (str): The name of a phone template in Bulk Administration -> Phones -> Phone Template

        Returns:
            dict: The parsed template data
        """
        template_data = self.get_phone(name=template_name)
        template_data.update({"class": "Phone"}, **kwargs)
        for value in ("lines", "loadInformation", "versionStamp"):
            if value in template_data:
                del template_data[value]

        result = self.__extract_template("addPhone", template_data, "phone")
        return result

    def _from_gateway_template(self, template_name: str, **kwargs) -> dict:
        """Generates template data from a given UCM gateway template. The template data can be used as a base to insert new gateways.

        Args:
            template_name (str): The name of a gateway template in Bulk Administration -> Gateways -> Gateway Template

        Returns:
            dict: The parsed template data
        """
        template_data = self.get_gateway(device_name=template_name)
        template_data.update(**kwargs)
        for value in ("versionStamp", "uuid", "loadInformation", "scratch"):
            if value in template_data:
                del template_data[value]

        return self.__extract_template("addGateway", template_data, child="gateway")

    def _from_endpoint_template(
        self,
        template_name: str,
        gw_domain_name: str,
        index: int,
        endpoint_kwargs: dict,
        line_pattern: str,
        line_route_partition: str,
        *,
        unit: int = 0,
        subunit: int = 0,
    ) -> dict:
        """Generates template data from a given gateway endpoint template. The template data can be used to populate new gateway endpoints.

        Args:
            template_name (str): The name of an endpoint template from a gateway template in Bulk Administration -> Gateways -> Gateway Template
            gw_domain_name (str): The full domain name of the gateway where this endpoint template will be used
            index (int): The position of the endpoint on the gateway subunit
            endpoint_kwargs (dict): Custom property values to be added to/changed on the endpoint template
            line_pattern (str): The pattern for the DN that will be used on this endpoint
            line_route_partition (str): The route partition for the DN that will be used on this enpoint
            unit (int, optional): The index of the unit where the endpoint will be placed. Defaults to 0.
            subunit (int, optional): The index of the subunit where the endpoint will be placed. Defaults to 0.

        Raises:
            InvalidArguments: when a supplied value for a pre-defined entry (gateway domain name, DN, etc) does not exist or is invalid

        Returns:
            dict: The parsed template data
        """
        template_data = self.get_endpoint(name=template_name)
        del template_data["gatewayUuid"]

        template_data.update(
            {
                "domainName": gw_domain_name,
                "unit": unit,
                "subunit": subunit,
            }
        )
        template_data["endpoint"].update(
            {
                "class": "Phone",
                "index": index,
                "name": f"AN{gw_domain_name.replace('SKIGW','')}{str(index).zfill(3)}",
            },
            **endpoint_kwargs,
        )

        try:
            self.get_directory_number(
                line_pattern, line_route_partition, return_tags=["pattern"]
            )
        except AXLFault:
            raise InvalidArguments(
                f"Cannot create endpoint with ({line_pattern}, {line_route_partition}): DN does not exist."
            )
        # insert lineIdentifier
        del template_data["endpoint"]["lines"]["line"]
        template_data["endpoint"]["lines"]["lineIdentifier"] = {
            "directoryNumber": line_pattern,
            "routePartitionName": line_route_partition,
        }

        return self.__extract_template(
            "addGatewaySccpEndpoints", template_data, child="gatewaySccpEndpoints"
        )

    def _from_line_template(
        self, template_name: str, template_route_partition: str, **kwargs
    ) -> dict:
        """Generates template data from a given UCM line template. The template data can be used as a base to insert new lines.

        Args:
            template_name (str): The name of a line template from one of the phone templates in Bulk Administration -> Phones -> Phone Template
            template_route_partition (str): The route partition of the template line

        Returns:
            dict: The parsed template data
        """
        template_data = self.get_directory_number(
            pattern=template_name,
            route_partition=template_route_partition,
            return_tags=[],
        )
        template_data.update({"active": "true", "usage": Nil}, **kwargs)

        result = self.__extract_template("addLine", template_data, "line")
        return result

    # *******************************
    # ----- [BASIC SOAP CALLS] -----
    # *******************************

    def _base_soap_call(
        self,
        element_name: str,
        msg_kwargs: dict,
        wanted_keys: list[str],
    ):
        try:
            result = getattr(self.client, element_name)(**msg_kwargs)
        except AttributeError:
            raise DumbProgrammerException(f"AXL has no element named {element_name}")
        except Fault as e:
            raise AXLFault(e)

        if type(result) == list:
            # ignore wanted keys since it's not a mapping
            return result

        for key in wanted_keys:
            try:
                result = result[key]
            except TypeError:
                raise DumbProgrammerException(
                    f"({element_name}, {wanted_keys=}) nothing to extract at '{key}'"
                )
            except KeyError:
                progress = (
                    "['" + "']['".join(wanted_keys[: wanted_keys.index(key)]) + "']"
                )
                raise DumbProgrammerException(
                    f"({element_name}, {wanted_keys=}) does not contain '{key}'{' at ' + progress if wanted_keys.index(key) > 0 else ''}"
                )

        return result

    def _base_soap_call_uuid(
        self,
        element_name: str,
        msg_kwargs: dict,
        wanted_keys: list[str],
        non_uuid_value="name",
    ):
        try:
            uuid_value = msg_kwargs["uuid"]
        except KeyError:
            raise DumbProgrammerException(
                f"({element_name}) 'uuid' not supplied as a kwarg"
            )
        if uuid_value:
            return self._base_soap_call(
                element_name,
                {k: v for k, v in msg_kwargs.items() if k != non_uuid_value},
                wanted_keys,
            )
        else:
            return self._base_soap_call(
                element_name,
                {k: v for k, v in msg_kwargs.items() if k != "uuid"},
                wanted_keys,
            )

    # *************************
    # ----- [OTHER TOOLS] -----
    # *************************

    def _multithread(
        self,
        method: Callable,
        kwargs_list: list[dict],
        catagorize_by=None,
        verbose=False,
    ):
        if verbose:
            print(f"Starting {method.__name__} multithreaded operation...")
            pbar = tqdm(total=len(kwargs_list))

        with ThreadPoolExecutor(max_workers=100) as ex:
            axl_futs = {ex.submit(method, **kw): kw for kw in kwargs_list}
            for fut in as_completed(axl_futs):
                if (exc := fut.exception()) is not None:
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise MultithreadException(method.__name__, axl_futs[fut], exc)
                if verbose:
                    pbar.update(1)

        if verbose:
            pbar.close()

        if catagorize_by is not None:
            return {kw[catagorize_by]: f.result() for f, kw in axl_futs.items()}
        else:
            return [f.result() for f in axl_futs.keys()]

    def print_axl_arguments(
        self, method_name: str, show_required_only=False, show_member_types=False
    ) -> None:
        """Prints out a tree of all available kwargs that can be supplied to a given method of this Axl class.

        Parameters
        ----------
        method_name : str
            A method (i.e. "get_phones") that is part of the Axl class
        show_required_members : bool, optional
            Option to only show which members are required for this method's API request, by default False
        show_member_types : bool, optional
            Option to show the accepted type for each member, by default False

        Raises
        ------
        AXLClassException
            when the method name provided isn't valid, or there isn't an associated AXL request (no XSD element)
        """
        method = getattr(self, method_name, None)
        if method is None:
            raise AXLClassException(
                f"'{method_name}' is not a valid method of the 'Axl' class"
            )

        if not hasattr(method, "check"):
            print(
                f"This method uses a standard argument format with other logic that makes the AXL API call for you. Please use the standard method arguments of:",
            )
            print_signature(method, "Axl")
        elif not hasattr(method, "element_name"):
            raise AXLClassException(
                f"'{method_name}' does not have an associated XSD element"
            )
        elif method.check == "tags":
            print(
                f"{colored('[NOTE]', 'yellow')}: The following tree only applies to the 'return_tags' argument, which will determine what data points are returned to you from the API call.",
                "\nFor instance, giving 'return_tags=[\"description\", \"model\"]' to Axl.get_phone() will result in only the 'description' and 'model' fields being returned.",
                "\nSmaller number of return tags can give you performance benefits in AXL API calls, but you may also use 'return_tags=[]' if you wish to receive data for all fields.\n",
                sep="",
            )
            print_return_tags_layout(
                self.zeep,
                method.element_name,
                show_required=True,
                show_types=show_member_types,
            )
        elif method.check == "args":
            if show_required_only:
                print_required_element_layout(
                    self.zeep, method.element_name, show_types=show_member_types
                )
            else:
                print_element_layout(
                    self.zeep,
                    method.element_name,
                    show_required=True,
                    show_types=show_member_types,
                )

    #########################
    # ===== SQL QUERIES =====
    #########################

    def run_sql_query(self, query: str) -> dict:
        """Legacy function. Use sql_query() instead.

        Parameters
        ----------
        query : str
            SQL query to be run.

        Returns
        -------
        dict
            Contains 'num_rows', 'query', and 'rows' only if the query returned anything. Otherwise, only returns 'num_rows' = 0 and 'query' = query.
        Fault
            The error returned from AXL, if one occured.
        """
        result = {"num_rows": 0, "query": query}

        try:
            sql_result = self.sql_query(query)
        except Exception as fault:
            sql_result = None
            self.last_exception = fault

        num_rows = 0
        result_rows = []

        if sql_result is not None:
            for row in sql_result["row"]:
                result_rows.append({})
                for column in row:
                    result_rows[num_rows][column.tag] = column.text
                num_rows += 1

        result["num_rows"] = num_rows
        if num_rows > 0:
            result["rows"] = result_rows

        return result

    def sql_query(self, query: str) -> Union[list[list[str]], Fault]:
        """Runs an SQL query on the UCM DB and returns the results.

        Parameters
        ----------
        query : str
            The SQL query to run. Do not include "run sql" in your query (as you would in the UCM CLI interface)

        Returns
        -------
        list[list[str]]
            The returned SQL rows in the form of a nested list, with the first "row" being the headers.
        Fault
            The error returned from AXL, if one occured.
        """
        try:
            recv = self.client.executeSQLQuery(query)["return"]
            data = recv["row"]
        except Fault as e:
            raise AXLFault(e)
        except (KeyError, TypeError):  # no rows returned
            return [[]]
        if not data:  # data is empty
            return [[]]

        # Zeep returns nested list of Element objs
        # Need to extract text from all Element objs
        parsed_data: list[list[str]] = []
        parsed_data.append([e.tag for e in data[0]])  # headers
        for row in data:
            parsed_data.append([e.text for e in row])

        return parsed_data

    def sql_update(self, query: str) -> dict:
        """Run an update on the UCM SQL DB.

        Parameters
        ----------
        query : str
            The SQL query to run. Do not include "run sql" in your query (as you would in the UCM CLI interface)

        Returns
        -------
        dict
            The response from AXL if all goes well
        Fault
            The error returned from AXL, if one occured
        """
        try:
            return self.client.executeSQLUpdate(query)["return"]
        except Fault as e:
            raise AXLFault(e)

    ##################
    # ===== LDAP =====
    ##################

    @serialize_list
    @check_tags("listLdapDirectory")
    def get_ldap_dir(
        self,
        *,
        return_tags=[
            "name",
            "ldapDn",
            "userSearchBase",
        ],
    ) -> Union[dict, None]:
        """Get LDAP syncs

        Parameters
        ----------
        return_tags : list, optional, keyword-only
            The categories to be returned, by default [ "name", "ldapDn", "userSearchBase", ]. If an empty list is provided, all categories will be returned.

        Returns
        -------
        dict
            The response from AXL if all goes well
        Fault
            The error returned from AXL, if one occured
        """
        tags = _tag_handler(return_tags)

        try:
            return self.client.listLdapDirectory({"name": "%"}, returnedTags=tags)[
                "return"
            ]["ldapDirectory"]
        except Fault as e:
            raise AXLFault(e)

    # ? don't want to do LDAP sync to test this one out...
    @serialize
    def do_ldap_sync(self, uuid):
        """
        Do LDAP Sync
        :param uuid: uuid
        :return: result dictionary
        """
        try:
            return self.client.doLdapSync(uuid=uuid, sync=True)
        except Fault as e:
            raise AXLFault(e)

    ############################
    # ===== DEVICE ACTIONS =====
    ############################

    @serialize
    @operation_tag("doChangeDNDStatus")
    def do_change_dnd_status(self, user_id: str, dnd_enabled: bool) -> dict:
        """Turns on/off DND for all devices associated with a given user.

        Parameters
        ----------
        user_id : str
            The user whose devices you want to change DND status
        dnd_enabled : bool
            True to turn on DND, False to turn it off

        Returns
        -------
        dict
            The response from AXL if all goes well
        Fault
            The error returned from AXL, if one occured
        """
        try:
            return self.client.doChangeDNDStatus(userID=user_id, dndStatus=dnd_enabled)
        except Fault as e:
            raise AXLFault(e)

    # ? no idea what this does
    @check_arguments("doDeviceLogin")
    def do_device_login(self, **kwargs):
        try:
            return self.client.doDeviceLogin(**kwargs)
        except Fault as e:
            raise AXLFault(e)

    # ? no idea what this does
    @check_arguments("doDeviceLogout")
    def do_device_logout(self, **kwargs):
        try:
            return self.client.doDeviceLogout(**kwargs)
        except Fault as e:
            raise AXLFault(e)

    @serialize
    @operation_tag("doDeviceReset")
    def do_device_reset(self, name="", uuid="") -> Union[dict, Fault, None]:
        """Sends a device reset to the requested phone. Same as pressing the "Reset" button on a phone in the UCM web interface.

        Parameters
        ----------
        name : str, optional
            The device name. If uuid is also provided, this value will be ignored.
        uuid : str, optional
            The uuid of the device. If provided, the name value will be ignored.

        Returns
        -------
        dict
            The response from AXL if all goes well.
        Fault
            The error returned from AXL, if one occurs.
        None
            If neither name nor uuid are supplied as parameters (no action taken).
        """
        if name != "" and uuid == "":
            try:
                return self.client.doDeviceReset(deviceName=name, isHardReset=True)
            except Fault as e:
                return e
        elif uuid != "":
            try:
                return self.client.doDeviceReset(uuid=uuid, isHardReset=True)
            except Fault as e:
                return e

    # ? can't risk testing this
    @operation_tag("resetSipTrunk")
    def reset_sip_trunk(self, name="", uuid=""):
        """
        Reset SIP Trunk
        :param name: device name
        :param uuid: device uuid
        :return: result dictionary
        """
        if name != "" and uuid == "":
            try:
                return self.client.resetSipTrunk(name=name)
            except Fault as e:
                return e
        elif name == "" and uuid != "":
            try:
                return self.client.resetSipTrunk(uuid=uuid)
            except Fault as e:
                return e

    #######################
    # ===== LOCATIONS =====
    #######################

    @serialize_list
    @check_tags(element_name="listLocation")
    def get_locations(
        self,
        name="%",
        *,
        return_tags=[
            "name",
            "withinAudioBandwidth",
            "withinVideoBandwidth",
            "withinImmersiveKbits",
        ],
    ) -> Union[list[dict], None]:
        """Get all locations created in UCM

        Parameters
        ----------
        name : str, optional
            Name to search against all locations, by default "%", the SQL "any" wildcard.
        return_tags : list, optional, keyword-only
            The categories to be returned, by default [ "name", "withinAudioBandwidth", "withinVideoBandwidth", "withinImmersiveKbits", ]. If an empty list is provided, all categories will be returned.

        Returns
        -------
        list[dict]
            A list of all location info.
        Fault
            The error returned from AXL, if one occured.
        """
        if return_tags and type(return_tags[0]) == dict:
            tags = return_tags[0]
        elif return_tags:
            tags = {t: "" for t in return_tags}

        try:
            return self.client.listLocation({"name": name}, returnedTags=tags,)[
                "return"
            ]["location"]
        except Fault as e:
            raise AXLFault(e)

    @serialize
    @operation_tag("getLocation")
    def get_location(self, name="", uuid="") -> Union[dict, Fault, None]:
        """Finds the requested location and returns data on that location.

        Parameters
        ----------
        name : str, optional
            Name of the location. If uuid is also provided, this value will be ignored.
        uuid : str, optional
            The uuid of the location. If provided, the name value will be ignored.

        Returns
        -------
        dict
            The information on the requested location.
        Fault
            The error returned from AXL, if one occurs.
        None
            If neither name nor uuid are supplied as parameters (no action taken).
        """
        if name != "" and uuid == "":
            try:
                return self.client.getLocation(name=name)
            except Fault as e:
                return e
        elif uuid != "":
            try:
                return self.client.getLocation(uuid=uuid)
            except Fault as e:
                return e
        else:
            return None

    # ! I'm definitely gonna need help with this one...
    def add_location(
        self,
        name: str,
        kbits=512,
        video_kbits=-1,
        within_audio_bw=512,
        within_video_bw=-1,
        within_immersive_kbits=-1,
    ):
        """
        Add a location
        :param name: Name of the location to add
        :param cucm_version: ucm version
        :param kbits: ucm 8.5
        :param video_kbits: ucm 8.5
        :param within_audio_bw: ucm 10
        :param within_video_bw: ucm 10
        :param within_immersive_kbits: ucm 10
        :return: result dictionary
        """
        if (
            self.cucm_version == "8.6"
            or self.cucm_version == "9.0"
            or self.cucm_version == "9.5"
            or self.cucm_version == "10.0"
        ):
            try:
                return self.client.addLocation(
                    {
                        "name": name,
                        # CUCM 8.6
                        "kbits": kbits,
                        "videoKbits": video_kbits,
                    }
                )
            except Fault as e:
                return e
        else:
            try:
                betweenLocations = []
                betweenLocation = {}
                RLocationBetween = {}
                RLocationBetween["locationName"] = "Hub_None"
                RLocationBetween["weight"] = 0
                RLocationBetween["audioBandwidth"] = within_audio_bw
                RLocationBetween["videoBandwidth"] = within_video_bw
                RLocationBetween["immersiveBandwidth"] = within_immersive_kbits
                betweenLocation["betweenLocation"] = RLocationBetween
                betweenLocations.append(betweenLocation)

                return self.client.addLocation(
                    {
                        "name": name,
                        # CUCM 10.6
                        "withinAudioBandwidth": within_audio_bw,
                        "withinVideoBandwidth": within_video_bw,
                        "withinImmersiveKbits": within_immersive_kbits,
                        "betweenLocations": betweenLocations,
                    }
                )
            except Fault as e:
                return e

    @operation_tag("removeLocation")
    def delete_location(self, name="", uuid=""):
        """Deletes the requested location.

        Parameters
        ----------
        name : str, optional
            Name of the location. If uuid is also provided, this value will be ignored.
        uuid : str, optional
            The uuid of the location. If provided, the name value will be ignored.

        Returns
        -------
        dict
            The completion information from AXL.
        Fault
            The error returned from AXL, if one occurs.
        None
            If neither name nor uuid are supplied as parameters (no action taken).
        """
        if name != "" and uuid == "":
            try:
                return self.client.removeLocation(name=name)
            except Fault as e:
                return e
        elif uuid != "":
            try:
                return self.client.removeLocation(uuid=uuid)
            except Fault as e:
                return e
        else:
            return None

    # ! gonna need help with this one too
    @check_arguments("updateLocation")
    def update_location(self, **kwargs):
        try:
            return self.client.updateLocation(**kwargs)
        except Fault as e:
            raise AXLFault(e)

    #####################
    # ===== REGIONS =====
    #####################

    @serialize_list
    @check_tags("listRegion")
    def get_regions(self, *, return_tags=[]) -> Union[list[dict], Fault]:
        """Gets a list of all regions on the current cluster. Note that the data that AXL will respond with is limited. Please used get_region() for a specific region if you wish to see more details.

        Parameters
        ----------
        return_tags : list, optional
            The categories to be returned, by default, []. If an empty list is provided, all categories will be returned.

        Returns
        -------
        list[dict]
            list of all regions found
        Fault
            the error returned by AXL upon a failed request
        """
        tags = _tag_handler(return_tags)
        try:
            return self.client.listRegion(
                searchCriteria={"name": "%"}, returnedTags=tags
            )["return"]["region"]
        except Fault as e:
            raise AXLFault(e)

    @serialize
    @check_tags("getRegion")
    def get_region(self, name: str, *, return_tags=["name", "relatedRegions"]):
        """
        Get region information
        :param name: Region name
        :return: result dictionary
        """
        tags = _tag_handler(
            return_tags
        )  # TODO: figure out why relatedRegion isn't expanded from @check_tags
        print(tags)
        try:
            return self.client.getRegion(name=name, returnedTags=tags)
        except Fault as e:
            raise AXLFault(e)

    def add_region(self, name):
        """
        Add a region
        :param name: Name of the region to add
        :return: result dictionary
        """
        try:
            return self.client.addRegion({"name": name})
        except Fault as e:
            raise AXLFault(e)

    def update_region(self, name="", newName="", moh_region=""):
        """
        Update region and assign region to all other regions
        :param name:
        :param uuid:
        :param moh_region:
        :return:
        """
        # Get all Regions
        all_regions = self.client.listRegion({"name": "%"}, returnedTags={"name": ""})
        # Make list of region names
        region_names = [str(i["name"]) for i in all_regions["return"]["region"]]
        # Build list of dictionaries to add to region api call
        region_list = []

        for i in region_names:
            # Highest codec within a region
            if i == name:
                region_list.append(
                    {
                        "regionName": i,
                        "bandwidth": "256 kbps",
                        "videoBandwidth": "-1",
                        "immersiveVideoBandwidth": "-1",
                        "lossyNetwork": "Use System Default",
                    }
                )

            # Music on hold region name
            elif i == moh_region:
                region_list.append(
                    {
                        "regionName": i,
                        "bandwidth": "64 kbps",
                        "videoBandwidth": "-1",
                        "immersiveVideoBandwidth": "-1",
                        "lossyNetwork": "Use System Default",
                    }
                )

            # All else G.711
            else:
                region_list.append(
                    {
                        "regionName": i,
                        "bandwidth": "64 kbps",
                        "videoBandwidth": "-1",
                        "immersiveVideoBandwidth": "-1",
                        "lossyNetwork": "Use System Default",
                    }
                )
        try:
            return self.client.updateRegion(
                name=name,
                newName=newName,
                relatedRegions={"relatedRegion": region_list},
            )
        except Fault as e:
            raise AXLFault(e)

    def delete_region(self, **args):
        """
        Delete a location
        :param name: The name of the region to delete
        :param uuid: The uuid of the region to delete
        :return: result dictionary
        """
        try:
            return self.client.removeRegion(**args)
        except Fault as e:
            raise AXLFault(e)

    ##################
    # ===== SRST =====
    ##################

    def get_srsts(self, tagfilter={"uuid": ""}):
        """
        Get all SRST details
        :param mini: return a list of tuples of SRST details
        :return: A list of dictionary's
        """
        try:
            return self.client.listSrst({"name": "%"}, returnedTags=tagfilter)[
                "return"
            ]["srst"]
        except Fault as e:
            raise AXLFault(e)

    def get_srst(self, name):
        """
        Get SRST information
        :param name: SRST name
        :return: result dictionary
        """
        try:
            return self.client.getSrst(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_srst(self, name, ip_address, port=2000, sip_port=5060):
        """
        Add SRST
        :param name: SRST name
        :param ip_address: SRST ip address
        :param port: SRST port
        :param sip_port: SIP port
        :return: result dictionary
        """
        try:
            return self.client.addSrst(
                {
                    "name": name,
                    "port": port,
                    "ipAddress": ip_address,
                    "SipPort": sip_port,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def delete_srst(self, name):
        """
        Delete a SRST
        :param name: The name of the SRST to delete
        :return: result dictionary
        """
        try:
            return self.client.removeSrst(name=name)
        except Fault as e:
            raise AXLFault(e)

    def update_srst(self, name, newName=""):
        """
        Update a SRST
        :param srst: The name of the SRST to update
        :param newName: The new name of the SRST
        :return: result dictionary
        """
        try:
            return self.client.updateSrst(name=name, newName=newName)
        except Fault as e:
            raise AXLFault(e)

    ##########################
    # ===== DEVICE POOLS =====
    ##########################

    def get_device_pools(
        self,
        tagfilter={
            "name": "",
            "dateTimeSettingName": "",
            "callManagerGroupName": "",
            "mediaResourceListName": "",
            "regionName": "",
            "srstName": "",
            # 'localRouteGroup': [0],
        },
    ):
        """
        Get a dictionary of device pools
        :param mini: return a list of tuples of device pool info
        :return: a list of dictionary's of device pools information
        """
        try:
            return self.client.listDevicePool({"name": "%"}, returnedTags=tagfilter,)[
                "return"
            ]["devicePool"]
        except Fault as e:
            raise AXLFault(e)

    def get_device_pool(self, **args):
        """
        Get device pool parameters
        :param name: device pool name
        :return: result dictionary
        """
        try:
            return self.client.getDevicePool(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_device_pool(
        self,
        name,
        date_time_group="CMLocal",
        region="Default",
        location="Hub_None",
        route_group="",
        media_resource_group_list="",
        srst="Disable",
        cm_group="Default",
        network_locale="",
    ):

        """
        Add a device pool
        :param device_pool: Device pool name
        :param date_time_group: Date time group name
        :param region: Region name
        :param location: Location name
        :param route_group: Route group name
        :param media_resource_group_list: Media resource group list name
        :param srst: SRST name
        :param cm_group: CM Group name
        :param network_locale: Network locale name
        :return: result dictionary
        """
        try:
            return self.client.addDevicePool(
                {
                    "name": name,
                    "dateTimeSettingName": date_time_group,  # update to state timezone
                    "regionName": region,
                    "locationName": location,
                    "localRouteGroup": {
                        "name": "Standard Local Route Group",
                        "value": route_group,
                    },
                    "mediaResourceListName": media_resource_group_list,
                    "srstName": srst,
                    "callManagerGroupName": cm_group,
                    "networkLocale": network_locale,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def update_device_pool(self, **args):
        """
        Update a device pools route group and media resource group list
        :param name:
        :param uuid:
        :param newName:
        :param mediaResourceGroupListName:
        :param dateTimeSettingName:
        :param callManagerGroupName:
        :param regionName:
        :param locationName:
        :param networkLocale:
        :param srstName:
        :param localRouteGroup:
        :param elinGroup:
        :param media_resource_group_list:
        :return:
        """
        try:
            return self.client.updateDevicePool(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_device_pool(self, **args):
        """
        Delete a Device pool
        :param device_pool: The name of the Device pool to delete
        :return: result dictionary
        """
        try:
            return self.client.removeDevicePool(**args)
        except Fault as e:
            raise AXLFault(e)

    ################################
    # ===== CONFERENCE BRIDGES =====
    ################################

    def get_conference_bridges(
        self,
        tagfilter={
            "name": "",
            "description": "",
            "devicePoolName": "",
            "locationName": "",
        },
    ):
        """
        Get conference bridges
        :param mini: List of tuples of conference bridge details
        :return: results dictionary
        """
        try:
            return self.client.listConferenceBridge(
                {"name": "%"},
                returnedTags=tagfilter,
            )["return"]["conferenceBridge"]
        except Fault as e:
            raise AXLFault(e)

    def get_conference_bridge(self, name):
        """
        Get conference bridge parameters
        :param name: conference bridge name
        :return: result dictionary
        """
        try:
            return self.client.getConferenceBridge(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_conference_bridge(
        self,
        name,
        description="",
        device_pool="Default",
        location="Hub_None",
        product="Cisco IOS Enhanced Conference Bridge",
        security_profile="Non Secure Conference Bridge",
    ):
        """
        Add a conference bridge
        :param conference_bridge: Conference bridge name
        :param description: Conference bridge description
        :param device_pool: Device pool name
        :param location: Location name
        :param product: Conference bridge type
        :param security_profile: Conference bridge security type
        :return: result dictionary
        """
        try:
            return self.client.addConferenceBridge(
                {
                    "name": name,
                    "description": description,
                    "devicePoolName": device_pool,
                    "locationName": location,
                    "product": product,
                    "securityProfileName": security_profile,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def update_conference_bridge(self, **args):
        """
        Update a conference bridge
        :param name: Conference bridge name
        :param newName: New Conference bridge name
        :param description: Conference bridge description
        :param device_pool: Device pool name
        :param location: Location name
        :param product: Conference bridge type
        :param security_profile: Conference bridge security type
        :return: result dictionary
        """
        try:
            return self.client.updateConferenceBridge(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_conference_bridge(self, name):
        """
        Delete a Conference bridge
        :param name: The name of the Conference bridge to delete
        :return: result dictionary
        """
        try:
            return self.client.removeConferenceBridge(name=name)
        except Fault as e:
            raise AXLFault(e)

    #########################
    # ===== TRANSCODERS =====
    #########################

    def get_transcoders(
        self, tagfilter={"name": "", "description": "", "devicePoolName": ""}
    ):
        """
        Get transcoders
        :param mini: List of tuples of transcoder details
        :return: results dictionary
        """
        try:
            return self.client.listTranscoder({"name": "%"}, returnedTags=tagfilter,)[
                "return"
            ]["transcoder"]
        except Fault as e:
            raise AXLFault(e)

    def get_transcoder(self, name):
        """
        Get conference bridge parameters
        :param name: transcoder name
        :return: result dictionary
        """
        try:
            return self.client.getTranscoder(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_transcoder(
        self,
        name,
        description="",
        device_pool="Default",
        product="Cisco IOS Enhanced Media Termination Point",
    ):
        """
        Add a transcoder
        :param transcoder: Transcoder name
        :param description: Transcoder description
        :param device_pool: Transcoder device pool
        :param product: Trancoder product
        :return: result dictionary
        """
        try:
            return self.client.addTranscoder(
                {
                    "name": name,
                    "description": description,
                    "devicePoolName": device_pool,
                    "product": product,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def update_transcoder(self, **args):
        """
        Add a transcoder
        :param name: Transcoder name
        :param newName: New Transcoder name
        :param description: Transcoder description
        :param device_pool: Transcoder device pool
        :param product: Trancoder product
        :return: result dictionary
        """
        try:
            return self.client.updateTranscoder(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_transcoder(self, name):
        """
        Delete a Transcoder
        :param name: The name of the Transcoder to delete
        :return: result dictionary
        """
        try:
            return self.client.removeTranscoder(name=name)
        except Fault as e:
            raise AXLFault(e)

    #################
    # ===== MTP =====
    #################

    def get_mtps(self, tagfilter={"name": "", "description": "", "devicePoolName": ""}):
        """
        Get mtps
        :param mini: List of tuples of transcoder details
        :return: results dictionary
        """
        try:
            return self.client.listMtp({"name": "%"}, returnedTags=tagfilter,)[
                "return"
            ]["mtp"]
        except Fault as e:
            raise AXLFault(e)

    def get_mtp(self, name):
        """
        Get mtp parameters
        :param name: transcoder name
        :return: result dictionary
        """
        try:
            return self.client.getMtp(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_mtp(
        self,
        name,
        description="",
        device_pool="Default",
        mtpType="Cisco IOS Enhanced Media Termination Point",
    ):
        """
        Add an mtp
        :param name: MTP name
        :param description: MTP description
        :param device_pool: MTP device pool
        :param mtpType: MTP Type
        :return: result dictionary
        """
        try:
            return self.client.addMtp(
                {
                    "name": name,
                    "description": description,
                    "devicePoolName": device_pool,
                    "mtpType": mtpType,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def update_mtp(self, **args):
        """
        Update an MTP
        :param name: MTP name
        :param newName: New MTP name
        :param description: MTP description
        :param device_pool: MTP device pool
        :param mtpType: MTP Type
        :return: result dictionary
        """
        try:
            return self.client.updateMtp(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_mtp(self, name):
        """
        Delete an MTP
        :param name: The name of the Transcoder to delete
        :return: result dictionary
        """
        try:
            return self.client.removeMtp(name=name)
        except Fault as e:
            raise AXLFault(e)

    ###########################
    # ===== H323 GATEWAYS =====
    ###########################

    def get_h323_gateways(
        self,
        tagfilter={
            "name": "",
            "description": "",
            "devicePoolName": "",
            "locationName": "",
            "sigDigits": "",
        },
    ):
        """
        Get H323 Gateways
        :param mini: List of tuples of H323 Gateway details
        :return: results dictionary
        """
        try:
            return self.client.listH323Gateway({"name": "%"}, returnedTags=tagfilter,)[
                "return"
            ]["h323Gateway"]
        except Fault as e:
            raise AXLFault(e)

    def get_h323_gateway(self, name):
        """
        Get H323 Gateway parameters
        :param name: H323 Gateway name
        :return: result dictionary
        """
        try:
            return self.client.getH323Gateway(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_h323_gateway(self, **args):
        """
        Add H323 gateway
        :param h323_gateway:
        :param description:
        :param device_pool:
        :param location:
        :param media_resource_group_list: Media resource group list name
        :param prefix_dn:
        :param sig_digits: Significant digits, 99 = ALL
        :param css:
        :param aar_css:
        :param aar_neighborhood:
        :param product:
        :param protocol:
        :param protocol_side:
        :param pstn_access:
        :param redirect_in_num_ie:
        :param redirect_out_num_ie:
        :param cld_party_ie_num_type:
        :param clng_party_ie_num_type:
        :param clng_party_nat_pre:
        :param clng_party_inat_prefix:
        :param clng_party_unknown_prefix:
        :param clng_party_sub_prefix:
        :param clng_party_nat_strip_digits:
        :param clng_party_inat_strip_digits:
        :param clng_party_unknown_strip_digits:
        :param clng_party_sub_strip_digits:
        :param clng_party_nat_trans_css:
        :param clng_party_inat_trans_css:
        :param clng_party_unknown_trans_css:
        :param clng_party_sub_trans_css:
        :return:
        """
        try:
            return self.client.addH323Gateway(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_h323_gateway(self, **args):
        """

        :param name:
        :return:
        """
        try:
            return self.client.updateH323Gateway(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_h323_gateway(self, name):
        """
        Delete a H323 gateway
        :param name: The name of the H323 gateway to delete
        :return: result dictionary
        """
        try:
            return self.client.removeH323Gateway(name=name)
        except Fault as e:
            raise AXLFault(e)

    ##########################
    # ===== ROUTE GROUPS =====
    ##########################

    def get_route_groups(self, tagfilter={"name": "", "distributionAlgorithm": ""}):
        """
        Get route groups
        :param mini: return a list of tuples of route group details
        :return: A list of dictionary's
        """
        try:
            return self.client.listRouteGroup({"name": "%"}, returnedTags=tagfilter)[
                "return"
            ]["routeGroup"]
        except Fault as e:
            raise AXLFault(e)

    def get_route_group(self, **args):
        """
        Get route group
        :param name: route group name
        :param uuid: route group uuid
        :return: result dictionary
        """
        try:
            return self.client.getRouteGroup(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_route_group(self, name, distribution_algorithm="Top Down", members=[]):
        """
        Add a route group
        :param name: Route group name
        :param distribution_algorithm: Top Down/Circular
        :param members: A list of devices to add (must already exist DUH!)
        """
        req = {
            "name": name,
            "distributionAlgorithm": distribution_algorithm,
            "members": {"member": []},
        }

        if members:
            [
                req["members"]["member"].append(
                    {
                        "deviceName": i,
                        "deviceSelectionOrder": members.index(i) + 1,
                        "port": 0,
                    }
                )
                for i in members
            ]

        try:
            return self.client.addRouteGroup(req)
        except Fault as e:
            raise AXLFault(e)

    def delete_route_group(self, **args):
        """
        Delete a Route group
        :param name: The name of the Route group to delete
        :return: result dictionary
        """
        try:
            return self.client.removeRouteGroup(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_route_group(self, **args):
        """
        Update a Route group
        :param name: The name of the Route group to update
        :param distribution_algorithm: Top Down/Circular
        :param members: A list of devices to add (must already exist DUH!)
        :return: result dictionary
        """
        try:
            return self.client.updateRouteGroup(**args)
        except Fault as e:
            raise AXLFault(e)

    #########################
    # ===== ROUTE LISTS =====
    #########################

    def get_route_lists(self, tagfilter={"name": "", "description": ""}):
        """
        Get route lists
        :param mini: return a list of tuples of route list details
        :return: A list of dictionary's
        """
        try:
            return self.client.listRouteList({"name": "%"}, returnedTags=tagfilter)[
                "return"
            ]["routeList"]
        except Fault as e:
            raise AXLFault(e)

    def get_route_list(self, **args):
        """
        Get route list
        :param name: route list name
        :param uuid: route list uuid
        :return: result dictionary
        """
        try:
            return self.client.getRouteList(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_route_list(
        self,
        name,
        description="",
        cm_group_name="Default",
        route_list_enabled="true",
        run_on_all_nodes="false",
        members=[],
    ):

        """
        Add a route list
        :param name: Route list name
        :param description: Route list description
        :param cm_group_name: Route list call mangaer group name
        :param route_list_enabled: Enable route list
        :param run_on_all_nodes: Run route list on all nodes
        :param members: A list of route groups
        :return: Result dictionary
        """
        req = {
            "name": name,
            "description": description,
            "callManagerGroupName": cm_group_name,
            "routeListEnabled": route_list_enabled,
            "runOnEveryNode": run_on_all_nodes,
            "members": {"member": []},
        }

        if members:
            [
                req["members"]["member"].append(
                    {
                        "routeGroupName": i,
                        "selectionOrder": members.index(i) + 1,
                        "calledPartyTransformationMask": "",
                        "callingPartyTransformationMask": "",
                        "digitDiscardInstructionName": "",
                        "callingPartyPrefixDigits": "",
                        "prefixDigitsOut": "",
                        "useFullyQualifiedCallingPartyNumber": "Default",
                        "callingPartyNumberingPlan": "Cisco CallManager",
                        "callingPartyNumberType": "Cisco CallManager",
                        "calledPartyNumberingPlan": "Cisco CallManager",
                        "calledPartyNumberType": "Cisco CallManager",
                    }
                )
                for i in members
            ]

        try:
            return self.client.addRouteList(req)
        except Fault as e:
            raise AXLFault(e)

    def delete_route_list(self, **args):
        """
        Delete a Route list
        :param name: The name of the Route list to delete
        :param uuid: The uuid of the Route list to delete
        :return: result dictionary
        """
        try:
            return self.client.removeRouteList(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_route_list(self, **args):
        """
        Update a Route list
        :param name: The name of the Route list to update
        :param uuid: The uuid of the Route list to update
        :param description: Route list description
        :param cm_group_name: Route list call mangaer group name
        :param route_list_enabled: Enable route list
        :param run_on_all_nodes: Run route list on all nodes
        :param members: A list of route groups
        :return: result dictionary
        """
        try:
            return self.client.updateRouteList(**args)
        except Fault as e:
            raise AXLFault(e)

    ##############################
    # ===== ROUTE PARTITIONS =====
    ##############################

    def get_partitions(self, tagfilter={"name": "", "description": ""}):
        """
        Get partitions
        :param mini: return a list of tuples of partition details
        :return: A list of dictionary's
        """
        try:
            return self.client.listRoutePartition(
                {"name": "%"}, returnedTags=tagfilter
            )["return"]["routePartition"]
        except Fault as e:
            raise AXLFault(e)

    def get_partition(self, **args):
        """
        Get partition details
        :param partition: Partition name
        :param uuid: UUID name
        :return: result dictionary
        """
        try:
            return self.client.getRoutePartition(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_partition(self, name, description="", time_schedule_name="All the time"):
        """
        Add a partition
        :param name: Name of the partition to add
        :param description: Partition description
        :param time_schedule_name: Name of the time schedule to use
        :return: result dictionary
        """
        try:
            return self.client.addRoutePartition(
                {
                    "name": name,
                    "description": description,
                    "timeScheduleIdName": time_schedule_name,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def delete_partition(self, **args):
        """
        Delete a partition
        :param partition: The name of the partition to delete
        :return: result dictionary
        """
        try:
            return self.client.removeRoutePartition(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_partition(self, **args):
        """
        Update calling search space
        :param uuid: CSS UUID
        :param name: CSS Name
        :param description:
        :param newName:
        :param timeScheduleIdName:
        :param useOriginatingDeviceTimeZone:
        :param timeZone:
        :return: result dictionary
        """
        try:
            return self.client.updateRoutePartition(**args)
        except Fault as e:
            raise AXLFault(e)

    #################
    # ===== CSS =====
    #################

    def get_calling_search_spaces(self, tagfilter={"name": "", "description": ""}):
        """
        Get calling search spaces
        :param mini: return a list of tuples of css details
        :return: A list of dictionary's
        """
        try:
            return self.client.listCss({"name": "%"}, returnedTags=tagfilter)["return"][
                "css"
            ]
        except Fault as e:
            raise AXLFault(e)

    def get_calling_search_space(self, **css):
        """
        Get Calling search space details
        :param name: Calling search space name
        :param uuid: Calling search space uuid
        :return: result dictionary
        """
        try:
            return self.client.getCss(**css)
        except Fault as e:
            raise AXLFault(e)

    def add_calling_search_space(self, name, description="", members=[]):
        """
        Add a Calling search space
        :param name: Name of the CSS to add
        :param description: Calling search space description
        :param members: A list of partitions to add to the CSS
        :return: result dictionary
        """
        req = {
            "name": name,
            "description": description,
            "members": {"member": []},
        }
        if members:
            [
                req["members"]["member"].append(
                    {
                        "routePartitionName": i,
                        "index": members.index(i) + 1,
                    }
                )
                for i in members
            ]

        try:
            return self.client.addCss(req)
        except Fault as e:
            raise AXLFault(e)

    def delete_calling_search_space(self, **args):
        """
        Delete a Calling search space
        :param calling_search_space: The name of the partition to delete
        :return: result dictionary
        """
        try:
            return self.client.removeCss(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_calling_search_space(self, **args):
        """
        Update calling search space
        :param uuid: CSS UUID
        :param name: CSS Name
        :param description:
        :param newName:
        :param members:
        :param removeMembers:
        :param addMembers:
        :return: result dictionary
        """
        try:
            return self.client.updateCss(**args)
        except Fault as e:
            raise AXLFault(e)

    ############################
    # ===== ROUTE PATTERNS =====
    ############################

    def get_route_patterns(
        self, tagfilter={"pattern": "", "description": "", "uuid": ""}
    ):
        """
        Get route patterns
        :param mini: return a list of tuples of route pattern details
        :return: A list of dictionary's
        """
        try:
            return self.client.listRoutePattern(
                {"pattern": "%"},
                returnedTags=tagfilter,
            )["return"]["routePattern"]
        except Fault as e:
            raise AXLFault(e)

    def get_route_pattern(self, pattern="", uuid=""):
        """
        Get route pattern
        :param pattern: route pattern
        :param uuid: route pattern uuid
        :return: result dictionary
        """
        if uuid == "" and pattern != "":
            # Cant get pattern directly so get UUID first
            try:
                uuid = self.client.listRoutePattern(
                    {"pattern": pattern}, returnedTags={"uuid": ""}
                )
            except Fault as e:
                return e
            if "return" in uuid and uuid["return"] is not None:
                uuid = uuid["return"]["routePattern"][0]["uuid"]
                try:
                    return self.client.getRoutePattern(uuid=uuid)
                except Fault as e:
                    return e

        elif uuid != "" and pattern == "":
            try:
                return self.client.getRoutePattern(uuid=uuid)
            except Fault as e:
                return e

    def add_route_pattern(
        self,
        pattern,
        gateway="",
        route_list="",
        description="",
        partition="",
        blockEnable=False,
        patternUrgency=False,
        releaseClause="Call Rejected",
    ):
        """
        Add a route pattern
        :param pattern: Route pattern - required
        :param gateway: Destination gateway - required
        :param route_list: Destination route list - required
               Either a gateway or route list can be used at the same time
        :param description: Route pattern description
        :param partition: Route pattern partition
        :return: result dictionary
        """

        req = {
            "pattern": pattern,
            "description": description,
            "destination": {},
            "routePartitionName": partition,
            "blockEnable": blockEnable,
            "releaseClause": releaseClause,
            "useCallingPartyPhoneMask": "Default",
            "networkLocation": "OnNet",
        }

        if gateway == "" and route_list == "":
            return "Either a gateway OR route list, is a required parameter"

        elif gateway != "" and route_list != "":
            return "Enter a gateway OR route list, not both"

        elif gateway != "":
            req["destination"].update({"gatewayName": gateway})
        elif route_list != "":
            req["destination"].update({"routeListName": route_list})
        try:
            return self.client.addRoutePattern(req)
        except Fault as e:
            raise AXLFault(e)

    def delete_route_pattern(self, **args):
        """
        Delete a route pattern
        :param uuid: The pattern uuid
        :param pattern: The pattern of the route to delete
        :param partition: The name of the partition
        :return: result dictionary
        """
        try:
            return self.client.removeRoutePattern(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_route_pattern(self, **args):
        """
        Update a route pattern
        :param uuid: The pattern uuid
        :param pattern: The pattern of the route to update
        :param partition: The name of the partition
        :param gateway: Destination gateway - required
        :param route_list: Destination route list - required
               Either a gateway or route list can be used at the same time
        :param description: Route pattern description
        :param partition: Route pattern partition
        :return: result dictionary
        """
        try:
            return self.client.updateRoutePattern(**args)
        except Fault as e:
            raise AXLFault(e)

    ###################################
    # ===== MEDIA RESOURCE GROUPS =====
    ###################################

    def get_media_resource_groups(self, tagfilter={"name": "", "description": ""}):
        """
        Get media resource groups
        :param mini: return a list of tuples of route pattern details
        :return: A list of dictionary's
        """
        try:
            return self.client.listMediaResourceGroup(
                {"name": "%"}, returnedTags=tagfilter
            )["return"]["mediaResourceGroup"]
        except Fault as e:
            raise AXLFault(e)

    def get_media_resource_group(self, name):
        """
        Get a media resource group details
        :param media_resource_group: Media resource group name
        :return: result dictionary
        """
        try:
            return self.client.getMediaResourceGroup(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_media_resource_group(
        self, name, description="", multicast="false", members=[]
    ):
        """
        Add a media resource group
        :param name: Media resource group name
        :param description: Media resource description
        :param multicast: Mulicast enabled
        :param members: Media resource group members
        :return: result dictionary
        """
        req = {
            "name": name,
            "description": description,
            "multicast": multicast,
            "members": {"member": []},
        }

        if members:
            [req["members"]["member"].append({"deviceName": i}) for i in members]

        try:
            return self.client.addMediaResourceGroup(req)
        except Fault as e:
            raise AXLFault(e)

    def update_media_resource_group(self, **args):
        """
        Update a media resource group
        :param name: Media resource group name
        :param description: Media resource description
        :param multicast: Mulicast enabled
        :param members: Media resource group members
        :return: result dictionary
        """
        try:
            return self.client.updateMediaResourceGroup(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_media_resource_group(self, name):
        """
        Delete a Media resource group
        :param media_resource_group: The name of the media resource group to delete
        :return: result dictionary
        """
        try:
            return self.client.removeMediaResourceGroup(name=name)
        except Fault as e:
            raise AXLFault(e)

    def get_media_resource_group_lists(self, tagfilter={"name": ""}):
        """
        Get media resource groups
        :param mini: return a list of tuples of route pattern details
        :return: A list of dictionary's
        """
        try:
            return self.client.listMediaResourceList(
                {"name": "%"}, returnedTags=tagfilter
            )["return"]["mediaResourceList"]
        except Fault as e:
            raise AXLFault(e)

    def get_media_resource_group_list(self, name):
        """
        Get a media resource group list details
        :param name: Media resource group list name
        :return: result dictionary
        """
        try:
            return self.client.getMediaResourceList(name=name)
        except Fault as e:
            raise AXLFault(e)

    def add_media_resource_group_list(self, name, members=[]):
        """
        Add a media resource group list
        :param media_resource_group_list: Media resource group list name
        :param members: A list of members
        :return:
        """
        req = {"name": name, "members": {"member": []}}

        if members:
            [
                req["members"]["member"].append(
                    {"order": members.index(i), "mediaResourceGroupName": i}
                )
                for i in members
            ]
        try:
            return self.client.addMediaResourceList(req)
        except Fault as e:
            raise AXLFault(e)

    def update_media_resource_group_list(self, **args):
        """
        Update a media resource group list
        :param name: Media resource group name
        :param description: Media resource description
        :param multicast: Mulicast enabled
        :param members: Media resource group members
        :return: result dictionary
        """
        try:
            return self.client.updateMediaResourceList(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_media_resource_group_list(self, name):
        """
        Delete a Media resource group list
        :param name: The name of the media resource group list to delete
        :return: result dictionary
        """
        try:
            return self.client.removeMediaResourceList(name=name)
        except Fault as e:
            raise AXLFault(e)

    ###############################
    # ===== DIRECTORY NUMBERS =====
    ###############################

    @serialize_list
    @check_tags("listLine")
    def get_directory_numbers(
        self,
        pattern="%",
        description="%",
        route_partition="%",
        *,
        return_tags=[
            "pattern",
            "description",
            "routePartitionName",
        ],
    ) -> Union[list[dict], Fault]:
        """Get all directory numbers that match the given criteria.

        Parameters
        ----------
        pattern : str, optional
            DN pattern to match against, by default "%" which is the SQL wildcard for "any"
        description : str, optional
            Description string to match against, by default "%" which is the SQL wildcard for "any"
        route_partition : str, optional
            Route partition name to match against, by default "%" which is the SQL wildcard for "any"
        return_tags : list, optional, keyword-only
            The categories to be returned, by default [ "pattern", "description", "routePartitionName", ]. If an empty list is provided, all categories will be returned.

        Returns
        -------
        list[dict]
            A list of all directory numbers found. List will be empty if no DNs are found.
        Fault
            If an error occurs, returns the error provided by AXL.
        """
        tags = _tag_handler(return_tags)

        return _chunk_data(
            self.client.listLine,
            data_label="line",
            searchCriteria={
                "pattern": pattern,
                "description": description,
                "routePartitionName": route_partition,
            },
            returnedTags=tags,
        )

    @serialize
    @check_tags("getLine")
    def get_directory_number(
        self,
        pattern: str,
        route_partition: str,
        *,
        return_tags=["pattern", "description", "routePartitionName"],
    ) -> dict:
        """Finds the DN matching the provided pattern and Route Partition.

        Parameters
        ----------
        pattern : str
            The digits of the DN. Must be exact, no SQL wildcards.
        route_partition : str
            The Route Partition where the DN can be found. Must be exact, no SQL wildcards.
        return_tags : list, optional, keyword-only
            The categories to be returned, by default ["pattern", "description", "routePartitionName"]. If an empty list is provided, all categories will be returned.

        Returns
        -------
        dict
            If the DN is found, returns requested data.
        Fault
            If the DN is not found or an error occurs, returns the error provided by AXL.
        """
        tags = _tag_handler(return_tags)
        try:
            return self.client.getLine(
                pattern=pattern, routePartitionName=route_partition, returnedTags=tags
            )["return"]["line"]
        except Fault as e:
            raise AXLFault(e)

    @check_arguments("addLine", child="line")
    def add_directory_number(
        self,
        pattern: str,
        route_partition: str,
        *,
        template_name=None,
        template_route_partition=None,
        **kwargs,
    ):
        # check pattern validity
        if not re.match(r"^[0-9\?\!\\\[\]\+\-\*\^\#X]+$", pattern):
            raise InvalidArguments(f"Invalid pattern '{pattern}'")

        # check route partition exists
        try:
            self.get_route_partition(name=route_partition, return_tags=["name"])
        except AXLFault as e:
            raise InvalidArguments(f"Route Partition {route_partition} does not exist")

        # check template
        if type(template_name) == str and type(template_route_partition) == str:
            try:
                template_line = self._from_line_template(
                    template_name,
                    template_route_partition,
                    pattern=pattern,
                    routePartitionName=route_partition,
                    **kwargs,
                )
            except AXLFault as e:
                if "Line was not found" in e.message:
                    raise AXLError(
                        f"Template '{template_name}' in {template_route_partition} could not be found",
                        e,
                    )
                else:
                    raise e
            return self._base_soap_call("addLine", {"line": template_line}, [])
        # no template
        else:
            return self._base_soap_call(
                "addLine",
                {
                    "pattern": pattern,
                    "routePartitionName": route_partition,
                    "usage": Nil,
                    **kwargs,
                },
                [],
            )

    @serialize
    @operation_tag("removeLine")
    def delete_directory_number(self, uuid="", pattern="", route_partition="") -> dict:
        """Attempts to delete a DN.

        Parameters
        ----------
        uuid : str, optional
            The ID value of the directory number provided by AXL. If uuid is provided, all other arguments will be ignored.
        pattern : str, optional
            The exact digits of the DN. If providing a pattern, must also provide route_partition.
        route_partition : str, optional
            The Route Partition where the DN can be found.

        Returns
        -------
        dict
            If no errors occured, returns the status code provided by AXL.
        Fault
            If an error occured, returns the error thrown by AXL.

        Raises
        ------
        InvalidArguments
            when either 'uuid' or a combination of 'pattern' and 'route_partition' aren't provided.
        """
        if uuid != "":
            try:
                return self.client.removeLine(uuid=uuid)
            except Fault as e:
                return e
        elif pattern != "" and route_partition != "":
            try:
                return self.client.removeLine(
                    pattern=pattern, routePartitionName=route_partition
                )
            except Fault as e:
                return e
        else:
            raise InvalidArguments(
                "If not using a uuid, both pattern and route_partition must be provided."
            )

    @serialize
    @operation_tag("updateLine")
    def update_directory_number(
        self, uuid="", pattern="", route_partition="", **kwargs
    ):
        """
        Update a directory number
        :param pattern: Directory number
        :param partition: Route partition name
        :param description: Directory number description
        :param alerting_name: Alerting name
        :param ascii_alerting_name: ASCII alerting name
        :param shared_line_css: Calling search space
        :param aar_neighbourhood: AAR group
        :param call_forward_css: Call forward calling search space
        :param vm_profile_name: Voice mail profile
        :param aar_destination_mask: AAR destination mask
        :param call_forward_destination: Call forward destination
        :param forward_all_to_vm: Forward all to voice mail checkbox
        :param forward_all_destination: Forward all destination
        :param forward_to_vm: Forward to voice mail checkbox
        :return: result dictionary
        """
        if uuid != "":
            try:
                return self.client.updateLine(uuid=uuid, **kwargs)
            except Fault as e:
                return e
        elif pattern != "" and route_partition != "":
            try:
                return self.client.updateLine(
                    pattern=pattern, route_partition=route_partition, **kwargs
                )
            except Fault as e:
                return e
        else:
            raise InvalidArguments(
                "If not using a uuid, both pattern and route_partition must be provided."
            )

    @serialize
    @check_tags("getRoutePartition")
    def get_route_partition(self, name="", uuid="", *, return_tags=[]) -> dict:
        tags = _tag_handler(return_tags)
        return self._base_soap_call_uuid(
            "getRoutePartition",
            {"name": name, "uuid": uuid, "returnedTags": tags},
            ["return", "routePartition"],
        )

    ##############################
    # ===== CTI ROUTE POINTS =====
    ##############################

    def get_cti_route_points(self, tagfilter={"name": "", "description": ""}):
        """
        Get CTI route points
        :param mini: return a list of tuples of CTI route point details
        :return: A list of dictionary's
        """
        try:
            return self.client.listCtiRoutePoint({"name": "%"}, returnedTags=tagfilter)[
                "return"
            ]["ctiRoutePoint"]
        except Fault as e:
            raise AXLFault(e)

    def get_cti_route_point(self, **args):
        """
        Get CTI route point details
        :param name: CTI route point name
        :param uuid: CTI route point uuid
        :return: result dictionary
        """
        try:
            return self.client.getCtiRoutePoint(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_cti_route_point(
        self,
        name,
        description="",
        device_pool="Default",
        location="Hub_None",
        common_device_config="",
        css="",
        product="CTI Route Point",
        dev_class="CTI Route Point",
        protocol="SCCP",
        protocol_slide="User",
        use_trusted_relay_point="Default",
        lines=[],
    ):
        """
        Add CTI route point
        lines should be a list of tuples containing the pattern and partition
        EG: [('77777', 'AU_PHONE_PT')]
        :param name: CTI route point name
        :param description: CTI route point description
        :param device_pool: Device pool name
        :param location: Location name
        :param common_device_config: Common device config name
        :param css: Calling search space name
        :param product: CTI device type
        :param dev_class: CTI device type
        :param protocol: CTI protocol
        :param protocol_slide: CTI protocol slide
        :param use_trusted_relay_point: Use trusted relay point: (Default, On, Off)
        :param lines: A list of tuples of [(directory_number, partition)]
        :return:
        """

        req = {
            "name": name,
            "description": description,
            "product": product,
            "class": dev_class,
            "protocol": protocol,
            "protocolSide": protocol_slide,
            "commonDeviceConfigName": common_device_config,
            "callingSearchSpaceName": css,
            "devicePoolName": device_pool,
            "locationName": location,
            "useTrustedRelayPoint": use_trusted_relay_point,
            "lines": {"line": []},
        }

        if lines:
            [
                req["lines"]["line"].append(
                    {
                        "index": lines.index(i) + 1,
                        "dirn": {"pattern": i[0], "routePartitionName": i[1]},
                    }
                )
                for i in lines
            ]

        try:
            return self.client.addCtiRoutePoint(req)
        except Fault as e:
            raise AXLFault(e)

    def delete_cti_route_point(self, **args):
        """
        Delete a CTI route point
        :param cti_route_point: The name of the CTI route point to delete
        :return: result dictionary
        """
        try:
            return self.client.removeCtiRoutePoint(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_cti_route_point(self, **args):
        """
        Add CTI route point
        lines should be a list of tuples containing the pattern and partition
        EG: [('77777', 'AU_PHONE_PT')]
        :param name: CTI route point name
        :param description: CTI route point description
        :param device_pool: Device pool name
        :param location: Location name
        :param common_device_config: Common device config name
        :param css: Calling search space name
        :param product: CTI device type
        :param dev_class: CTI device type
        :param protocol: CTI protocol
        :param protocol_slide: CTI protocol slide
        :param use_trusted_relay_point: Use trusted relay point: (Default, On, Off)
        :param lines: A list of tuples of [(directory_number, partition)]
        :return:
        """
        try:
            return self.client.updateCtiRoutePoint(**args)
        except Fault as e:
            raise AXLFault(e)

    ####################
    # ===== PHONES =====
    ####################

    @serialize_list
    @check_tags("listPhone")
    def get_phones(
        self,
        name="%",
        description="%",
        css="%",
        device_pool="%",
        security_profile="%",
        *,
        return_tags=[
            "name",
            "product",
            "description",
            "protocol",
            "locationName",
            "callingSearchSpaceName",
        ],
    ) -> list[dict]:
        tags: dict = _tag_handler(return_tags)

        return _chunk_data(
            self.client.listPhone,
            data_label="phone",
            searchCriteria={
                "name": name,
                "description": description,
                "callingSearchSpaceName": css,
                "devicePoolName": device_pool,
                "securityProfileName": security_profile,
            },
            returnedTags=tags,
        )

    @serialize
    @check_tags("getPhone")
    def get_phone(self, uuid="", name="", *, return_tags=[]):
        """
        Get device profile parameters
        :param phone: profile name
        :return: result dictionary
        """
        tags = _tag_handler(return_tags)

        if uuid != "":
            try:
                return self.client.getPhone(uuid=uuid, returnedTags=tags)["return"][
                    "phone"
                ]
            except Fault as e:
                return e
        elif name != "":
            try:
                return self.client.getPhone(name=name, returnedTags=tags)["return"][
                    "phone"
                ]
            except Fault as e:
                return e
        else:
            raise InvalidArguments("Must provide a value for either 'uuid' or 'name'")

    @serialize_list
    def get_phone_lines(
        self, uuid="", name="", main_line_only=False, *, return_tags=[]
    ):
        dev = self.get_phone(uuid=uuid, name=name, return_tags=["lines"])
        if dev["lines"] is None:
            return []

        lines = [
            (dn["dirn"]["pattern"], dn["dirn"]["routePartitionName"])
            for dn in dev["lines"]["line"]
        ]

        if main_line_only or len(lines) == 1:
            return [
                self.get_directory_number(
                    pattern=lines[0][0],
                    route_partition=lines[0][1],
                    return_tags=return_tags,
                )
            ]
        else:
            return self._multithread(
                method=self.get_directory_number,
                kwargs_list=[
                    {
                        "pattern": line[0],
                        "route_partition": line[1],
                        "return_tags": return_tags,
                    }
                    for line in lines
                ],
            )

    @check_arguments("addPhone", child="phone")
    def add_phone(
        self,
        dev_name: str,
        description: str,
        dev_model="",
        button_template="",
        dev_pool="",
        use_phone_template="",
        keep_template_load=False,
        *,
        protocol="SIP",
        common_phone_profile="Standard Common Phone Profile",
        location="Hub_None",
        use_relay_point="Default",
        built_in_bridge="Default",
        packet_capture_mode="None",
        cert_operation="No Pending Operation",
        mobility_mode="Default",
        **kwargs,
    ) -> dict:
        if use_phone_template:
            found_template = self.get_phone(name=use_phone_template)
            add_tags = self.__extract_template(
                "addPhone", found_template, child="phone"
            )
            add_tags.update(
                {
                    "name": dev_name,  # definitely want to use our own name
                    "class": "Phone",  # we're not inserting a "Phone Template" here
                    "description": description,
                }
            )

            add_tags.pop("lines")
            if not keep_template_load:
                add_tags.pop("loadInformation")

            # consider putting in the work to check the original method signature and see
            # if any keyword values are different than the default

        else:
            if any(
                [v == "" for v in (dev_model, description, button_template, dev_pool)]
            ):
                raise InvalidArguments(
                    "If a template is not used, values must be supplied for dev_model, description, button_template, and dev_pool"
                )

            add_tags = {
                "name": dev_name,
                "description": description,
                "product": dev_model,
                "class": "Phone",
                "protocol": protocol,
                "protocolSide": "User",
                "devicePoolName": dev_pool,
                "commonPhoneConfigName": common_phone_profile,
                "locationName": location,
                "useTrustedRelayPoint": use_relay_point,
                "phoneTemplateName": button_template,
                "primaryPhoneName": Nil,
                "builtInBridgeStatus": built_in_bridge,
                "packetCaptureMode": packet_capture_mode,
                "certificateOperation": cert_operation,
                "deviceMobilityMode": mobility_mode,
            }

        add_tags.update(kwargs)
        try:
            return self.client.addPhone(phone=add_tags)
        except Fault as e:
            raise AXLFault(e)

    def delete_phone(self, **args):
        """
        Delete a phone
        :param phone: The name of the phone to delete
        :return: result dictionary
        """
        try:
            return self.client.removePhone(**args)
        except Fault as e:
            raise AXLFault(e)

    @check_arguments("updatePhone")
    def update_phone(
        self,
        name: str,
        description="",
        css="",
        device_pool="",
        button_template="",
        softkey_template="",
        owner_user="",
        digest_user="",
        **kwargs,
    ):
        axl_args = filter_empty_kwargs(
            locals(),
            {
                "css": "callingSearchSpaceName",
                "device_pool": "devicePoolName",
                "button_template": "phoneTemplateName",
                "softkey_template": "softkeyTemplateName",
                "owner_user": "ownerUserName",
                "digest_user": "digestUser",
            },
        )

        if "name" not in axl_args:
            raise AXLException("'name' value can not be empty!")

        if (
            user := axl_args.get("ownerUserName", None)
        ) is not None and user.lower() == "anonymous":
            axl_args["ownerUserName"] = ""

        try:
            return self.client.updatePhone(**axl_args)
        except Fault as e:
            raise AXLFault(e)

    def add_phone_line(
        self, dev_name: str, dn: tuple[str, str], position=0, replace=False
    ):
        # get current device lines
        try:
            device = self.get_phone(name=dev_name, return_tags=["lines"])
        except AXLFault as e:
            raise AXLFaultHandler(f"Could not find phone {dev_name} to add line to:", e)

        # check if dn exists
        try:
            self.get_directory_number(*dn)
        except AXLFault as e:
            raise AXLFaultHandler(f"Could not find line {dn} to add to {dev_name}:", e)

        # convert lines to lineIdentifier format
        dn_to_id = lambda x: {"directoryNumber": x[0], "routePartitionName": x[1]}
        if (line_list := device["lines"]) is not None:
            original_dns = [
                (d["dirn"]["pattern"], d["dirn"]["routePartitionName"])
                for d in line_list["line"]
            ]
            line_ids = [dn_to_id(d) for d in original_dns]
        else:
            line_ids = []

        # insert new line into list
        if position == 0:
            line_ids.append(dn_to_id(dn))
        else:
            line_ids.insert(position - 1, dn_to_id(dn))

        # replace device lines with new list
        try:
            self.client.updatePhone(
                name=dev_name,
                lines={"lineIdentifier": line_ids},
            )
        except Fault as e:
            # ugly but don't care right now
            raise AXLFaultHandler(
                f"Could not add line {dn} to {dev_name}:", AXLFault(e)
            )
        except Exception as e:
            raise AXLError(
                f"Could not add line {dn} to {dev_name} due to an unknown error:", e
            )

    def remove_phone_line(self, dev_name: str, dn=None, index=0, cascade=True):
        # make sure user chose something
        if not any(dn, index):
            raise InvalidArguments(
                f"Must provide either a DN pattern or a phone line index"
            )
        elif all(dn, index):
            raise InvalidArguments(f"Please only provide either a DN or a line index")

        # get phone lines
        try:
            device = self.get_phone(name=dev_name, return_tags=["lines"])
        except AXLFault as e:
            raise AXLFaultHandler(
                f"Could not find phone {dev_name} to remove line from:", e
            )

        if (original_lines := device["lines"]) is None:
            return None

        # find matching line
        if dn:
            match_dn = (
                lambda x: x["dirn"]["pattern"] == dn[0]
                and x["dirn"]["routePartitionName"] == dn[1]
            )
            matches = filter(match_dn, original_lines["line"])
            if not matches:
                print(f"(Couldn't find {dn} in {dev_name} to delete, skipping...)")
            else:
                to_delete = matches[0]
        else:
            by_index = lambda x: x["index"]
            ordered_lines = sorted(original_lines["line"], key=by_index)
            if index > len(ordered_lines):
                raise Exception(
                    f"{dev_name} only has {len(ordered_lines)}, can't delete Index {index}"
                )
            else:
                to_delete = ordered_lines[index - 1]

        # ! not finished

    def update_phone_line(self):
        pass

    def add_phone_speeddials(self):
        pass

    def remove_phone_speeddials(self, pattern="", index=0, cascade=True):
        pass

    def update_phone_speeddials(self):
        pass

    def add_phone_blf(self):
        pass

    def remove_phone_blf(self, pattern="", route_partition="", index=0, cascade=True):
        pass

    def update_phone_blf(self):
        pass

    #############################
    # ===== DEVICE PROFILES =====
    #############################

    def get_device_profiles(
        self,
        tagfilter={
            "name": "",
            "product": "",
            "protocol": "",
            "phoneTemplateName": "",
        },
    ):
        """
        Get device profile details
        :param mini: return a list of tuples of device profile details
        :return: A list of dictionary's
        """
        try:
            return self.client.listDeviceProfile(
                {"name": "%"},
                returnedTags=tagfilter,
            )["return"]["deviceProfile"]
        except Fault as e:
            raise AXLFault(e)

    def get_device_profile(self, **args):
        """
        Get device profile parameters
        :param name: profile name
        :param uuid: profile uuid
        :return: result dictionary
        """
        try:
            return self.client.getDeviceProfile(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_device_profile(
        self,
        name,
        description="",
        product="Cisco 7962",
        phone_template="Standard 7962G SCCP",
        dev_class="Device Profile",
        protocol="SCCP",
        protocolSide="User",
        softkey_template="Standard User",
        em_service_name="Extension Mobility",
        lines=[],
    ):
        """
        Add A Device profile for use with extension mobility
        lines takes a list of Tuples with properties for each line EG:

                                               display                           external
            DN     partition    display        ascii          label               mask
        [('77777', 'LINE_PT', 'Jim Smith', 'Jim Smith', 'Jim Smith - 77777', '0294127777')]
        :param name:
        :param description:
        :param product:
        :param phone_template:
        :param lines:
        :param dev_class:
        :param protocol:
        :param softkey_template:
        :param em_service_name:
        :return:
        """

        req = {
            "name": name,
            "description": description,
            "product": product,
            "class": dev_class,
            "protocol": protocol,
            "protocolSide": protocolSide,
            "softkeyTemplateName": softkey_template,
            "phoneTemplateName": phone_template,
            "lines": {"line": []},
        }

        if lines:
            [
                req["lines"]["line"].append(
                    {
                        "index": lines.index(i) + 1,
                        "dirn": {"pattern": i[0], "routePartitionName": i[1]},
                        "display": i[2],
                        "displayAscii": i[3],
                        "label": i[4],
                        "e164Mask": i[5],
                    }
                )
                for i in lines
            ]

        try:
            blah = self.client.addDeviceProfile(req)
            return blah
        except Fault as e:
            raise AXLFault(e)

    def delete_device_profile(self, **args):
        """
        Delete a device profile
        :param profile: The name of the device profile to delete
        :return: result dictionary
        """
        try:
            return self.client.removeDeviceProfile(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_device_profile(self, **args):
        """
        Update A Device profile for use with extension mobility
        lines takes a list of Tuples with properties for each line EG:

                                               display                           external
            DN     partition    display        ascii          label               mask
        [('77777', 'LINE_PT', 'Jim Smith', 'Jim Smith', 'Jim Smith - 77777', '0294127777')]
        :param profile:
        :param description:
        :param product:
        :param phone_template:
        :param lines:
        :param dev_class:
        :param protocol:
        :param softkey_template:
        :param em_service_name:
        :return:
        """
        try:
            return self.client.updateDeviceProfile(**args)
        except Fault as e:
            raise AXLFault(e)

    ###################
    # ===== USERS =====
    ###################

    def get_users(self, tagfilter={"userid": "", "firstName": "", "lastName": ""}):
        """
        Get users details
        :return: A list of dictionary's
        """
        skip = 0
        a = []

        def inner(skip):
            while True:
                res = self.client.listUser(
                    {"userid": "%"}, returnedTags=tagfilter, first=1000, skip=skip
                )["return"]
                skip = skip + 1000
                if res is not None and "user" in res:
                    yield res["user"]
                else:
                    break

        for each in inner(skip):
            a.extend(each)
        return a

    def get_user(self, userid):
        """
        Get user parameters
        :param user_id: profile name
        :return: result dictionary
        """
        try:
            return self.client.getUser(userid=userid)["return"]["user"]
        except Fault as e:
            raise AXLFault(e)

    def add_user(
        self,
        userid,
        lastName,
        firstName,
        presenceGroupName="Standard Presence group",
        phoneProfiles=[],
    ):
        """
        Add a user
        :param user_id: User ID of the user to add
        :param first_name: First name of the user to add
        :param last_name: Last name of the user to add
        :return: result dictionary
        """

        try:
            return self.client.addUser(
                {
                    "userid": userid,
                    "lastName": lastName,
                    "firstName": firstName,
                    "presenceGroupName": presenceGroupName,
                    "phoneProfiles": phoneProfiles,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def update_user(self, **args):
        """
        Update end user for credentials
        :param userid: User ID
        :param password: Web interface password
        :param pin: Extension mobility PIN
        :return: result dictionary
        """
        try:
            return self.client.updateUser(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_user_em(
        self, user_id, device_profile, default_profile, subscribe_css, primary_extension
    ):
        """
        Update end user for extension mobility
        :param user_id: User ID
        :param device_profile: Device profile name
        :param default_profile: Default profile name
        :param subscribe_css: Subscribe CSS
        :param primary_extension: Primary extension, must be a number from the device profile
        :return: result dictionary
        """
        try:
            resp = self.client.getDeviceProfile(name=device_profile)
        except Fault as e:
            raise AXLFault(e)
        if "return" in resp and resp["return"] is not None:
            uuid = resp["return"]["deviceProfile"]["uuid"]
            try:
                return self.client.updateUser(
                    userid=user_id,
                    phoneProfiles={"profileName": {"uuid": uuid}},
                    defaultProfile=default_profile,
                    subscribeCallingSearchSpaceName=subscribe_css,
                    primaryExtension={"pattern": primary_extension},
                    associatedGroups={"userGroup": {"name": "Standard CCM End Users"}},
                )
            except Fault as e:
                return e
        else:
            return "Device Profile not found for user"

    def update_user_credentials(self, userid, password="", pin=""):
        """
        Update end user for credentials
        :param userid: User ID
        :param password: Web interface password
        :param pin: Extension mobility PIN
        :return: result dictionary
        """

        if password == "" and pin == "":
            return "Password and/or Pin are required"

        elif password != "" and pin != "":
            try:
                return self.client.updateUser(userid=userid, password=password, pin=pin)
            except Fault as e:
                return e

        elif password != "":
            try:
                return self.client.updateUser(userid=userid, password=password)
            except Fault as e:
                return e

        elif pin != "":
            try:
                return self.client.updateUser(userid=userid, pin=pin)
            except Fault as e:
                return e

    def delete_user(self, **args):
        """
        Delete a user
        :param userid: The name of the user to delete
        :return: result dictionary
        """
        try:
            return self.client.removeUser(**args)
        except Fault as e:
            raise AXLFault(e)

    ##################################
    # ===== TRANSLATION PATTERNS =====
    ##################################

    def get_translations(self):
        """
        Get translation patterns
        :param mini: return a list of tuples of route pattern details
        :return: A list of dictionary's
        """
        try:
            return self.client.listTransPattern(
                {"pattern": "%"},
                returnedTags={
                    "pattern": "",
                    "description": "",
                    "uuid": "",
                    "routePartitionName": "",
                    "callingSearchSpaceName": "",
                    "useCallingPartyPhoneMask": "",
                    "patternUrgency": "",
                    "provideOutsideDialtone": "",
                    "prefixDigitsOut": "",
                    "calledPartyTransformationMask": "",
                    "callingPartyTransformationMask": "",
                    "digitDiscardInstructionName": "",
                    "callingPartyPrefixDigits": "",
                    "provideOutsideDialtone": "",
                },
            )["return"]["transPattern"]
        except Fault as e:
            raise AXLFault(e)

    def get_translation(self, pattern="", routePartitionName="", uuid=""):
        """
        Get translation pattern
        :param pattern: translation pattern to match
        :param routePartitionName: routePartitionName required if searching pattern
        :param uuid: translation pattern uuid
        :return: result dictionary
        """

        if pattern != "" and routePartitionName != "" and uuid == "":
            try:
                return self.client.getTransPattern(
                    pattern=pattern,
                    routePartitionName=routePartitionName,
                    returnedTags={
                        "pattern": "",
                        "description": "",
                        "routePartitionName": "",
                        "callingSearchSpaceName": "",
                        "useCallingPartyPhoneMask": "",
                        "patternUrgency": "",
                        "provideOutsideDialtone": "",
                        "prefixDigitsOut": "",
                        "calledPartyTransformationMask": "",
                        "callingPartyTransformationMask": "",
                        "digitDiscardInstructionName": "",
                        "callingPartyPrefixDigits": "",
                    },
                )
            except Fault as e:
                return e
        elif uuid != "" and pattern == "" and routePartitionName == "":
            try:
                return self.client.getTransPattern(
                    uuid=uuid,
                    returnedTags={
                        "pattern": "",
                        "description": "",
                        "routePartitionName": "",
                        "callingSearchSpaceName": "",
                        "useCallingPartyPhoneMask": "",
                        "patternUrgency": "",
                        "provideOutsideDialtone": "",
                        "prefixDigitsOut": "",
                        "calledPartyTransformationMask": "",
                        "callingPartyTransformationMask": "",
                        "digitDiscardInstructionName": "",
                        "callingPartyPrefixDigits": "",
                    },
                )
            except Fault as e:
                return e
        else:
            return "must specify either uuid OR pattern and partition"

    def add_translation(
        self,
        pattern,
        partition,
        description="",
        usage="Translation",
        callingSearchSpaceName="",
        useCallingPartyPhoneMask="Off",
        patternUrgency="f",
        provideOutsideDialtone="f",
        prefixDigitsOut="",
        calledPartyTransformationMask="",
        callingPartyTransformationMask="",
        digitDiscardInstructionName="",
        callingPartyPrefixDigits="",
        blockEnable="f",
        routeNextHopByCgpn="f",
    ):
        """
        Add a translation pattern
        :param pattern: Translation pattern
        :param partition: Route Partition
        :param description: Description - optional
        :param usage: Usage
        :param callingSearchSpaceName: Calling Search Space - optional
        :param patternUrgency: Pattern Urgency - optional
        :param provideOutsideDialtone: Provide Outside Dial Tone - optional
        :param prefixDigitsOut: Prefix Digits Out - optional
        :param calledPartyTransformationMask: - optional
        :param callingPartyTransformationMask: - optional
        :param digitDiscardInstructionName: - optional
        :param callingPartyPrefixDigits: - optional
        :param blockEnable: - optional
        :return: result dictionary
        """
        try:
            return self.client.addTransPattern(
                {
                    "pattern": pattern,
                    "description": description,
                    "routePartitionName": partition,
                    "usage": usage,
                    "callingSearchSpaceName": callingSearchSpaceName,
                    "useCallingPartyPhoneMask": useCallingPartyPhoneMask,
                    "patternUrgency": patternUrgency,
                    "provideOutsideDialtone": provideOutsideDialtone,
                    "prefixDigitsOut": prefixDigitsOut,
                    "calledPartyTransformationMask": calledPartyTransformationMask,
                    "callingPartyTransformationMask": callingPartyTransformationMask,
                    "digitDiscardInstructionName": digitDiscardInstructionName,
                    "callingPartyPrefixDigits": callingPartyPrefixDigits,
                    "blockEnable": blockEnable,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def delete_translation(self, pattern="", partition="", uuid=""):
        """
        Delete a translation pattern
        :param pattern: The pattern of the route to delete
        :param partition: The name of the partition
        :param uuid: Required if pattern and partition are not specified
        :return: result dictionary
        """

        if pattern != "" and partition != "" and uuid == "":
            try:
                return self.client.removeTransPattern(
                    pattern=pattern, routePartitionName=partition
                )
            except Fault as e:
                return e
        elif uuid != "" and pattern == "" and partition == "":
            try:
                return self.client.removeTransPattern(uuid=uuid)
            except Fault as e:
                return e
        else:
            return "must specify either uuid OR pattern and partition"

    def update_translation(
        self,
        pattern="",
        partition="",
        uuid="",
        newPattern="",
        description="",
        newRoutePartitionName="",
        callingSearchSpaceName="",
        useCallingPartyPhoneMask="",
        patternUrgency="",
        provideOutsideDialtone="",
        prefixDigitsOut="",
        calledPartyTransformationMask="",
        callingPartyTransformationMask="",
        digitDiscardInstructionName="",
        callingPartyPrefixDigits="",
        blockEnable="",
    ):
        """
        Update a translation pattern
        :param uuid: UUID or Translation + Partition Required
        :param pattern: Translation pattern
        :param partition: Route Partition
        :param description: Description - optional
        :param usage: Usage
        :param callingSearchSpaceName: Calling Search Space - optional
        :param patternUrgency: Pattern Urgency - optional
        :param provideOutsideDialtone: Provide Outside Dial Tone - optional
        :param prefixDigitsOut: Prefix Digits Out - optional
        :param calledPartyTransformationMask: - optional
        :param callingPartyTransformationMask: - optional
        :param digitDiscardInstructionName: - optional
        :param callingPartyPrefixDigits: - optional
        :param blockEnable: - optional
        :return: result dictionary
        """

        args = {}
        if description != "":
            args["description"] = description
        if pattern != "" and partition != "" and uuid == "":
            args["pattern"] = pattern
            args["routePartitionName"] = partition
        if pattern == "" and partition == "" and uuid != "":
            args["uuid"] = uuid
        if newPattern != "":
            args["newPattern"] = newPattern
        if newRoutePartitionName != "":
            args["newRoutePartitionName"] = newRoutePartitionName
        if callingSearchSpaceName != "":
            args["callingSearchSpaceName"] = callingSearchSpaceName
        if useCallingPartyPhoneMask != "":
            args["useCallingPartyPhoneMask"] = useCallingPartyPhoneMask
        if digitDiscardInstructionName != "":
            args["digitDiscardInstructionName"] = digitDiscardInstructionName
        if callingPartyTransformationMask != "":
            args["callingPartyTransformationMask"] = callingPartyTransformationMask
        if calledPartyTransformationMask != "":
            args["calledPartyTransformationMask"] = calledPartyTransformationMask
        if patternUrgency != "":
            args["patternUrgency"] = patternUrgency
        if provideOutsideDialtone != "":
            args["provideOutsideDialtone"] = provideOutsideDialtone
        if prefixDigitsOut != "":
            args["prefixDigitsOut"] = prefixDigitsOut
        if callingPartyPrefixDigits != "":
            args["callingPartyPrefixDigits"] = callingPartyPrefixDigits
        if blockEnable != "":
            args["blockEnable"] = blockEnable
        try:
            return self.client.updateTransPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    ########################
    # ===== ROUTE PLAN =====
    ########################

    def list_route_plan(self, pattern=""):
        """
        List Route Plan
        :param pattern: Route Plan Contains Pattern
        :return: results dictionary
        """
        try:
            return self.client.listRoutePlan(
                {"dnOrPattern": "%" + pattern + "%"},
                returnedTags={
                    "dnOrPattern": "",
                    "partition": "",
                    "type": "",
                    "routeDetail": "",
                },
            )["return"]["routePlan"]
        except Fault as e:
            raise AXLFault(e)

    def list_route_plan_specific(self, pattern=""):
        """
        List Route Plan
        :param pattern: Route Plan Contains Pattern
        :return: results dictionary
        """
        try:
            return self.client.listRoutePlan(
                {"dnOrPattern": pattern},
                returnedTags={
                    "dnOrPattern": "",
                    "partition": "",
                    "type": "",
                    "routeDetail": "",
                },
            )
        except Fault as e:
            raise AXLFault(e)

    def get_called_party_xforms(self):
        """
        Get called party xforms
        :param mini: return a list of tuples of called party transformation pattern details
        :return: A list of dictionary's
        """
        try:
            return self.client.listCalledPartyTransformationPattern(
                {"pattern": "%"},
                returnedTags={"pattern": "", "description": "", "uuid": ""},
            )["return"]["calledPartyTransformationPattern"]
        except Fault as e:
            raise AXLFault(e)

    def get_called_party_xform(self, **args):
        """
        Get called party xform details
        :param name:
        :param partition:
        :param uuid:
        :return: result dictionary
        """
        try:
            return self.client.getCalledPartyTransformationPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_called_party_xform(
        self,
        pattern="",
        description="",
        partition="",
        calledPartyPrefixDigits="",
        calledPartyTransformationMask="",
        digitDiscardInstructionName="",
    ):
        """
        Add a called party transformation pattern
        :param pattern: pattern - required
        :param routePartitionName: partition required
        :param description: Route pattern description
        :param calledPartyTransformationmask:
        :param dialPlanName:
        :param digitDiscardInstructionName:
        :param routeFilterName:
        :param calledPartyPrefixDigits:
        :param calledPartyNumberingPlan:
        :param calledPartyNumberType:
        :param mlppPreemptionDisabled: does anyone use this?
        :return: result dictionary
        """
        try:
            return self.client.addCalledPartyTransformationPattern(
                {
                    "pattern": pattern,
                    "description": description,
                    "routePartitionName": partition,
                    "calledPartyPrefixDigits": calledPartyPrefixDigits,
                    "calledPartyTransformationMask": calledPartyTransformationMask,
                    "digitDiscardInstructionName": digitDiscardInstructionName,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def delete_called_party_xform(self, **args):
        """
        Delete a called party transformation pattern
        :param uuid: The pattern uuid
        :param pattern: The pattern of the transformation to delete
        :param partition: The name of the partition
        :return: result dictionary
        """
        try:
            return self.client.removeCalledPartyTransformationPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_called_party_xform(self, **args):
        """
        Update a called party transformation
        :param uuid: required unless pattern and routePartitionName is given
        :param pattern: pattern - required
        :param routePartitionName: partition required
        :param description: Route pattern description
        :param calledPartyTransformationmask:
        :param dialPlanName:
        :param digitDiscardInstructionName:
        :param routeFilterName:
        :param calledPartyPrefixDigits:
        :param calledPartyNumberingPlan:
        :param calledPartyNumberType:
        :param mlppPreemptionDisabled: does anyone use this?
        :return: result dictionary
        :return: result dictionary
        """
        try:
            return self.client.updateCalledPartyTransformationPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    def get_calling_party_xforms(self):
        """
        Get calling party xforms
        :param mini: return a list of tuples of calling party transformation pattern details
        :return: A list of dictionary's
        """
        try:
            return self.client.listCallingPartyTransformationPattern(
                {"pattern": "%"},
                returnedTags={"pattern": "", "description": "", "uuid": ""},
            )["return"]["callingPartyTransformationPattern"]
        except Fault as e:
            raise AXLFault(e)

    def get_calling_party_xform(self, **args):
        """
        Get calling party xform details
        :param name:
        :param partition:
        :param uuid:
        :return: result dictionary
        """
        try:
            return self.client.getCallingPartyTransformationPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    def add_calling_party_xform(
        self,
        pattern="",
        description="",
        partition="",
        callingPartyPrefixDigits="",
        callingPartyTransformationMask="",
        digitDiscardInstructionName="",
    ):
        """
        Add a calling party transformation pattern
        :param pattern: pattern - required
        :param routePartitionName: partition required
        :param description: Route pattern description
        :param callingPartyTransformationmask:
        :param dialPlanName:
        :param digitDiscardInstructionName:
        :param routeFilterName:
        :param callingPartyPrefixDigits:
        :param callingPartyNumberingPlan:
        :param callingPartyNumberType:
        :param mlppPreemptionDisabled: does anyone use this?
        :return: result dictionary
        """
        try:
            return self.client.addCallingPartyTransformationPattern(
                {
                    "pattern": pattern,
                    "description": description,
                    "routePartitionName": partition,
                    "callingPartyPrefixDigits": callingPartyPrefixDigits,
                    "callingPartyTransformationMask": callingPartyTransformationMask,
                    "digitDiscardInstructionName": digitDiscardInstructionName,
                }
            )
        except Fault as e:
            raise AXLFault(e)

    def delete_calling_party_xform(self, **args):
        """
        Delete a calling party transformation pattern
        :param uuid: The pattern uuid
        :param pattern: The pattern of the transformation to delete
        :param partition: The name of the partition
        :return: result dictionary
        """
        try:
            return self.client.removeCallingPartyTransformationPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_calling_party_xform(self, **args):
        """
        Update a calling party transformation
        :param uuid: required unless pattern and routePartitionName is given
        :param pattern: pattern - required
        :param routePartitionName: partition required
        :param description: Route pattern description
        :param callingPartyTransformationMask:
        :param dialPlanName:
        :param digitDiscardInstructionName:
        :param routeFilterName:
        :param calledPartyPrefixDigits:
        :param calledPartyNumberingPlan:
        :param calledPartyNumberType:
        :param mlppPreemptionDisabled: does anyone use this?
        :return: result dictionary
        :return: result dictionary
        """
        try:
            return self.client.updateCallingPartyTransformationPattern(**args)
        except Fault as e:
            raise AXLFault(e)

    ####################
    # ===== TRUNKS =====
    ####################

    def get_sip_trunks(
        self, tagfilter={"name": "", "sipProfileName": "", "callingSearchSpaceName": ""}
    ):
        try:
            return self.client.listSipTrunk({"name": "%"}, returnedTags=tagfilter)[
                "return"
            ]["sipTrunk"]
        except Fault as e:
            raise AXLFault(e)

    def get_sip_trunk(self, **args):
        """
        Get sip trunk
        :param name:
        :param uuid:
        :return: result dictionary
        """
        try:
            return self.client.getSipTrunk(**args)
        except Fault as e:
            raise AXLFault(e)

    def update_sip_trunk(self, **args):
        """
        Update a SIP Trunk
        :param name:
        :param uuid:
        :param newName:
        :param description:
        :param callingSearchSpaceName:
        :param devicePoolName:
        :param locationName:
        :param sipProfileName:
        :param mtpRequired:

        :return:
        """
        try:
            return self.client.updateSipTrunk(**args)
        except Fault as e:
            raise AXLFault(e)

    def delete_sip_trunk(self, **args):
        try:
            return self.client.removeSipTrunk(**args)
        except Fault as e:
            raise AXLFault(e)

    def get_sip_security_profile(self, name):
        try:
            return self.client.getSipTrunkSecurityProfile(name=name)["return"]
        except Fault as e:
            raise AXLFault(e)

    def get_sip_profile(self, name):
        try:
            return self.client.getSipProfile(name=name)["return"]
        except Fault as e:
            raise AXLFault(e)

    def add_sip_trunk(self, **args):
        """
        Add a SIP Trunk
        :param name:
        :param description:
        :param product:
        :param protocol:
        :param protocolSide:
        :param callingSearchSpaceName:
        :param devicePoolName:
        :param securityProfileName:
        :param sipProfileName:
        :param destinations: param destination:
        :param runOnEveryNode:

        :return:
        """
        try:
            return self.client.addSipTrunk(**args)
        except Fault as e:
            raise AXLFault(e)

    #################################
    # ===== SERVERS & CM GROUPS =====
    #################################

    def list_process_nodes(self):
        try:
            return self.client.listProcessNode(
                {"name": "%", "processNodeRole": "CUCM Voice/Video"},
                returnedTags={"name": ""},
            )["return"]["processNode"]
        except Fault as e:
            raise AXLFault(e)

    def add_call_manager_group(self, name, members):
        """
        Add call manager group
        :param name: name of cmg
        :param members[]: array of members
        :return: result dictionary
        """

        try:
            return self.client.addCallManagerGroup({"name": name, "members": members})
        except Fault as e:
            raise AXLFault(e)

    def get_call_manager_group(self, name):
        """
        Get call manager group
        :param name: name of cmg
        :return: result dictionary
        """
        try:
            return self.client.getCallManagerGroup(name=name)
        except Fault as e:
            raise AXLFault(e)

    def get_call_manager_groups(self):
        """
        Get call manager groups
        :param name: name of cmg
        :return: result dictionary
        """
        try:
            return self.client.listCallManagerGroup(
                {"name": "%"}, returnedTags={"name": ""}
            )["return"]["callManagerGroup"]
        except Fault as e:
            raise AXLFault(e)

    def update_call_manager_group(self, **args):
        """
        Update call manager group
        :param name: name of cmg
        :return: result dictionary
        """
        try:
            return self.client.listCallManagerGroup({**args}, returnedTags={"name": ""})
        except Fault as e:
            raise AXLFault(e)

    def delete_call_manager_group(self, name):
        """
        Delete call manager group
        :param name: name of cmg
        :return: result dictionary
        """
        try:
            return self.client.removeCallManagerGroup({"name": name})
        except Fault as e:
            raise AXLFault(e)

    ###########################
    # ===== SCCP GATEWAYS =====
    ###########################

    @serialize
    @check_tags("getGateway")
    def get_gateway(self, device_name="", uuid="", *, return_tags=[]):
        tags = _tag_handler(return_tags)
        return self._base_soap_call_uuid(
            element_name="getGateway",
            msg_kwargs={
                "domainName": device_name,
                "uuid": uuid,
                "returnedTags": tags,
            },
            wanted_keys=["return", "gateway"],
            non_uuid_value="domainName",
        )

    @serialize
    @check_tags("getGatewaySccpEndpoints")
    def get_endpoint(self, name="", uuid="", *, return_tags=[]):
        tags = _tag_handler(return_tags)
        return self._base_soap_call_uuid(
            element_name="getGatewaySccpEndpoints",
            msg_kwargs={
                "name": name,
                "uuid": uuid,
                "returnedTags": tags,
            },
            wanted_keys=["return", "gatewaySccpEndpoints"],
            non_uuid_value="name",
        )

    @serialize_list
    def get_gateway_endpoints(
        self,
        device_name="",
        uuid="",
        verbose=False,
        *,
        return_tags=[],
    ):
        gw = self.get_gateway(device_name, uuid, return_tags=["domainName"])
        mac_short = gw["domainName"].replace("SKIGW", "")
        an_devices = self.get_phones(name=f"AN{mac_short}%", return_tags=["name"])

        if not an_devices:
            return []

        lines = self._multithread(
            self.get_phone_lines,
            [
                {
                    "name": dev["name"],
                    "main_line_only": True,
                    "return_tags": return_tags,
                }
                for dev in an_devices
            ],
            catagorize_by="name",
            verbose=verbose,
        )

        results = []
        for dev in an_devices:
            line = lines.get(dev["name"], None)
            if not line:  # accounts for empty list and None value
                results.append({})
            else:
                results.append(line[0])

        return results

    def add_gateway(
        self,
        mac: str,
        description: str,
        model: str,
        cm_group: str,
        protocol="SCCP",
        **kwargs,
    ):
        if protocol.upper() not in ("SCCP", "MGCP"):
            raise InvalidArguments(f"{protocol} is not a valid gateway protocol")
        if len(mac) not in (10, 12):
            raise InvalidArguments(
                f"mac must be either full 12-digit MAC address or first 10-digits ({mac=})"
            )

        if model == "VG204":
            domain_name = "SKIGW" + (mac if len(mac) == 10 else mac[-10:])
            subunit = [{"index": 0, "product": "4FXS-SCCP", "beginPort": 0}]
            unit = [{"index": 0, "product": "ANALOG", "subunits": {"subunit": subunit}}]
            units = {"unit": unit}
        else:
            raise InvalidArguments(
                f"Sorry, this model of gateway is not supported yet: {model}"
            )

        gateway = {
            "domainName": domain_name,
            "description": description,
            "product": model,
            "protocol": protocol.upper(),
            "callManagerGroupName": cm_group,
            "units": units,
            **kwargs,
        }

        return self._base_soap_call("addGateway", {"gateway": gateway}, [])

    @check_arguments("addGateway", child="gateway")
    def add_gateway_from_template(
        self, mac: str, description: str, template_name: str, **kwargs
    ):
        if len(mac) not in (10, 12):
            raise InvalidArguments(
                f"mac must be either full 12-digit MAC address or first 10-digits ({mac=})"
            )

        gateway = self._from_gateway_template(
            template_name,
            domainName="SKIGW" + (mac if len(mac) == 10 else mac[-10:]),
            description=description,
            **kwargs,
        )

        return self._base_soap_call("addGateway", {"gateway": gateway}, [])

    @check_arguments("addGatewaySccpEndpoints", child="gatewaySccpEndpoints")
    def add_gateway_endpoint(
        self,
        host_domain_name: str,
        css: str,
        device_pool: str,
        location: str,
        phone_button_template="Standard Analog",
        security_profile="Analog Phone - Standard SCCP Non-Secure Proifle",
        mobility="Default",
        common_phone_config="Standard Common Phone Profile",
        subscribe_css: str = None,
        presence_group="Standard Presence group",
        unit: int = None,
        subunit: int = None,
        index: int = None,
    ):
        # get host gw info
        gateway = self.get_gateway(host_domain_name, return_tags=["protocol", "units"])
        protocol = gateway["protocol"]

        endpoint = {
            "domainName": host_domain_name,
            "unit": unit if unit is not None else 0,
            "subunit": subunit if subunit is not None else 0,
            # TODO: finish this up later
        }

    @check_arguments("addGatewaySccpEndpoints", child="gatewaySccpEndpoints")
    def add_gateway_endpoint_from_template(
        self,
        host_domain_name: str,
        endpoint_template: str,
        unit: int = None,
        subunit: int = None,
        index: int = None,
    ):
        # get gateway info
        try:
            units = self.get_gateway(host_domain_name, return_tags=["units"])["units"][
                "unit"
            ]
        except TypeError:
            raise InvalidArguments(
                f"{host_domain_name} does not have initialized Units"
            )

        # TODO: finish this up later

    #########################
    # ===== LINE GROUPS =====
    #########################

    @serialize
    @check_tags("getLineGroup")
    def get_line_group(self, name: str, *, return_tags=[]) -> dict:
        tags = _tag_handler(return_tags)
        try:
            return self.client.getLineGroup(name=name, returnedTags=tags)["return"][
                "lineGroup"
            ]
        except Fault as e:
            raise AXLFault(e)

    @serialize_list
    @check_tags("listLineGroup")
    def list_line_groups(self, *, return_tags=[]) -> list[dict]:
        tags = _tag_handler(return_tags)
        try:
            return self.client.listLineGroup(searchCriteria="%", returnedTags=tags)[
                "return"
            ]["lineGroup"]
        except TypeError:
            return []
        except Fault as e:
            raise AXLFault(e)

    def do_reset_line_group_devices(
        self, lg_name: str, stagger_timer=0.0, verbose=True
    ) -> int:
        try:
            lg = self.get_line_group(lg_name, return_tags=["members"])
        except AXLFault as e:
            raise AXLFaultHandler(f"Could not find Line Group with name '{lg_name}'", e)
        if lg["members"] is None:
            if verbose:
                print(f"[reset {lg_name} devices]: no devices to reset, skipping")
            return None

        device_count: int = 0
        for line in lg["members"]["member"]:
            dn = (
                line["directoryNumber"]["pattern"],
                line["directoryNumber"]["routePartitionName"],
            )
            line_devices = self.get_directory_number(
                *dn, return_tags=["associatedDevices"]
            )["associatedDevices"]
            if line_devices is None:
                if verbose:
                    print(f"(no devices found for {dn}, skipping...)")
            else:
                for device_name in line_devices["device"]:
                    self.do_device_reset(name=device_name)
                    device_count += 1
            print(f"({dn} complete)")
            if stagger_timer > 0.0 and line != lg["members"]["member"][-1]:
                sleep(stagger_timer)
        if verbose:
            print(f"Line Group '{lg_name}' reset complete")
        return device_count

    def do_reset_all_line_groups_devices(self, stagger_timer=1.0) -> None:
        lgs = [g["name"] for g in self.list_line_groups(return_tags=["name"])]

        print(f"Resetting devices in {len(lgs)} Line Groups...")
        with ThreadPoolExecutor(max_workers=100) as ex:
            lg_futs = {
                ex.submit(
                    self.do_reset_line_group_devices,
                    lg,
                    stagger_timer=stagger_timer,
                    verbose=False,
                ): lg
                for lg in lgs
            }
            for i, f in enumerate(as_completed(lg_futs)):
                if (exc := f.exception()) is not None:
                    print(
                        f"[{i}/{len(lgs)}] LG '{lg_futs[f]}' raised an exception: {exc}"  # TODO: color this red
                    )
                else:
                    print(
                        f"[{i}/{len(lgs)}] Reset '{lg_futs[f]}' with {f.result()} devices."
                    )

    @check_tags("getGatewaySccpEndpoints")
    def tag_test(a="", *, return_tags=[]):
        tags = _tag_handler(return_tags)

        def print_tags(t: dict, spacing=0):
            for k, v in t.items():
                if type(v) == dict:
                    print(f"{' '*spacing}{k}:")
                    print(f"{' '*spacing}{'{'}")
                    print_tags(v, spacing + 2)
                    print(f"{' '*spacing}{'}'}")
                else:
                    print(f"{' '*spacing}{k}: {v}")

        print_tags(tags)


# ****************************
# ----- UTILITY FUCTIONS -----
# ****************************


def _tag_handler(tags: list) -> dict:
    """Internal function for handling basic and complex return tag lists. Do not use.

    Parameters
    ----------
    tags : list
        A list of str tag names, or a list containing a single dict of all tags

    Returns
    -------
    dict
        A dict with properly formatted tags for Zeep
    """
    if tags and type(tags[0]) == dict:
        return tags[0]
    elif all([bool(type(t) == str) for t in tags]):
        return {t: "" for t in tags}


def _tag_serialize_filter(tags: Union[list, dict], data: dict) -> dict:
    """[summary]

    Parameters
    ----------
    tags : Union[list, dict]
        [description]
    data : dict
        [description]

    Returns
    -------
    dict
        [description]
    """

    def check_value(d: dict) -> dict:
        d_copy = d.copy()
        for tag, value in d_copy.items():
            if type(value) == dict:
                if "_value_1" in value:
                    d_copy[tag] = value["_value_1"]
                else:
                    d_copy[tag] = check_value(value)
            elif type(value) == list:
                for i, d in enumerate(deepcopy(value)):
                    if type(d) == dict:
                        value[i] = check_value(d)
        return d_copy

    working_data = deepcopy(data)
    for tag, value in data.items():
        if tag not in tags and len(tags) > 0 and value is None:
            working_data.pop(tag, None)
        elif type(value) == dict:
            if "_value_1" in value:
                working_data[tag] = value["_value_1"]
            else:
                working_data[tag] = check_value(value)
        elif type(value) == list:
            for i, d in enumerate(deepcopy(value)):
                if type(d) == dict:
                    value[i] = check_value(d)
    return working_data


def _chunk_data(axl_request: Callable, data_label: str, **kwargs) -> Union[list, Fault]:
    skip = 0
    recv: dict = dict()
    data: list = []

    while recv is not None:
        try:
            recv = axl_request(**kwargs, first=1000, skip=skip)["return"]
        except Fault as e:
            raise AXLFault(e)
        if recv is not None:
            data.extend(recv[data_label])
            skip += 1000
    return data


def filter_empty_kwargs(all_args: dict, arg_renames: dict = {}) -> dict:
    args_copy = all_args.copy()
    for arg, value in all_args.items():
        if value == "" or arg in ("self", "args", "kwargs"):
            args_copy.pop(arg)
        elif arg in arg_renames:
            args_copy[arg_renames[arg]] = args_copy.pop(arg)

        if value == Empty:
            args_copy[arg] = ""
    return args_copy
