from cucm.axl import Axl, get_credentials
from cucm.axl.exceptions import AXLClassException, UCMException
import keyring
import sys, os

ucm = None
while ucm is None:
    if not (weburl := keyring.get_password("cucm-py", "webaddr")):
        new_weburl = input("Please enter your CUCM URL (without port): ")
        try:
            ucm = Axl(*get_credentials(), cucm=new_weburl, verbose=True)
            keyring.set_password("cucm-py", "webaddr", new_weburl)
        except UCMException as e:
            print(f"\nThat URL didn't work ({e.err})...please try again.")
    else:
        try:
            ucm = Axl(*get_credentials(), cucm=weburl)
        except UCMException as e:
            if (
                input(
                    f"Stored URL '{weburl}' did not work ({e.err}).\nWant to try another? [y/n]: "
                ).lower()
                == "y"
            ):
                keyring.set_password("cucm-py", "webaddr", "")
                continue
            else:
                raise Exception("Could not connect to UCM AXL service")


def axl_connect() -> None:
    print(ucm.cucm, f"v{ucm.cucm_version}")


def print_axl_tree() -> None:
    if len(sys.argv) < 2:
        print(
            "USAGE: poetry run show_tree [AXL_METHOD] [AXL_METHOD_2] [AXL_METHOD_3] ..."
        )
    else:
        for n, method in enumerate(sys.argv[1:]):
            if n > 0:
                input("\nPress [enter] to continue or [ctrl + c] to stop.")
                print("\n", "=" * (os.get_terminal_size().columns - 1), sep="")

            try:
                print("")  # newline
                ucm.print_axl_arguments(method)
            except AXLClassException as e:
                print(f"[ERROR]({method}): {e.__str__}")