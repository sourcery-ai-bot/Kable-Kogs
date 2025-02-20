"""Script to automatically generate info.json files
and generate class docstrings from single info.yaml file for whole repo.

DISCLAIMER: While this script works, it uses some hacks and I don't recommend using it
if you don't understand how it does some stuff and why it does it like this.

---

Copyright 2018-2020 Jakub Kuczys (https://github.com/jack1142)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import json
import re
import string
import subprocess
import sys
import typing
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import parso
from redbot import VersionInfo
from strictyaml import (
    Bool,
    EmptyDict,
    EmptyList,
    Enum,
    Map,
    MapPattern,
    Optional,
    Regex,
    ScalarValidator,
    Seq,
    Str,
    Url,
)
from strictyaml import load as yaml_load
from strictyaml.exceptions import YAMLSerializationError, YAMLValidationError
from strictyaml.utils import is_string
from strictyaml.yamllocation import YAMLChunk

ROOT_PATH = Path(__file__).absolute().parent.parent

# `FormatPlaceholder`, `FormatDict` and `safe_format_alt` taken from
# https://stackoverflow.com/posts/comments/100958805


class FormatPlaceholder:
    def __init__(self, key):
        self.key = key

    def __format__(self, spec):
        result = self.key
        if spec:
            result += ":" + spec
        return "{" + result + "}"

    def __getitem__(self, index):
        self.key = f"{self.key}[{index}]"
        return self

    def __getattr__(self, attr):
        self.key = f"{self.key}.{attr}"
        return self


class FormatDict(dict):
    def __missing__(self, key):
        return FormatPlaceholder(key)


def safe_format_alt(text, source):
    formatter = string.Formatter()
    return formatter.vformat(text, (), FormatDict(source))


class PythonVersion(ScalarValidator):
    REGEX = re.compile(r"(\d+)\.(\d+)\.(\d+)")

    def __init__(self) -> None:
        self._matching_message = "when expecting Python version (MAJOR.MINOR.MICRO)"

    def validate_scalar(self, chunk: YAMLChunk) -> typing.List[int]:
        match = self.REGEX.fullmatch(chunk.contents)
        if match is None:
            raise YAMLValidationError(self._matching_message, "found non-matching string", chunk)
        return [int(group) for group in match.group(1, 2, 3)]

    def to_yaml(self, data: typing.Any) -> str:
        if isinstance(data, Sequence):
            if len(data) != 3:
                raise YAMLSerializationError(
                    f"expected a sequence of 3 elements, got {len(data)} elements"
                )
            for item in data:
                if not isinstance(item, int):
                    raise YAMLSerializationError(
                        f"expected int, got '{item}' of type '{type(item).__name__}'"
                    )
                if item < 0:
                    raise YAMLSerializationError(f"expected non-negative int, got {item}")
            return ".".join(str(segment) for segment in data)
        if is_string(data):
            # we just validated that it's a string
            version_string = typing.cast(str, data)
            if self.REGEX.fullmatch(version_string) is None:
                raise YAMLSerializationError(
                    "expected Python version (MAJOR.MINOR.MICRO)," f" got '{version_string}'"
                )
            return version_string
        raise YAMLSerializationError(
            "expected string or sequence," f" got '{data}' of type '{type(data).__name__}'"
        )


# TODO: allow author in COG_KEYS and merge them with repo/shared fields lists
REPO_KEYS = {
    "name": Str(),  # Downloader doesn't use this but I can set friendlier name
    "short": Str(),
    "description": Str(),
    "install_msg": Str(),
    "author": Seq(Str()),
}
COMMON_KEYS = {
    Optional("min_bot_version"): Regex(VersionInfo._VERSION_STR_PATTERN),
    Optional("max_bot_version"): Regex(VersionInfo._VERSION_STR_PATTERN),
    Optional("min_python_version"): PythonVersion(),
    Optional("hidden", False): Bool(),
    Optional("disabled", False): Bool(),
    Optional("type", "COG"): Enum(["COG", "SHARED_LIBRARY"]),
}
SHARED_FIELDS_KEYS = {
    "install_msg": Str(),
    "author": Seq(Str()),
    **COMMON_KEYS,
}
COG_KEYS = {
    "name": Str(),  # Downloader doesn't use this but I can set friendlier name
    "short": Str(),
    "description": Str(),
    "end_user_data_statement": Str(),
    Optional("class_docstring"): Str(),
    Optional("install_msg"): Str(),
    Optional("author"): Seq(Str()),
    Optional("required_cogs", {}): EmptyDict() | MapPattern(Str(), Url()),
    Optional("requirements", []): EmptyList() | Seq(Str()),
    Optional("tags", []): EmptyList() | Seq(Str()),
    **COMMON_KEYS,
}
SCHEMA = Map(
    {
        "repo": Map(REPO_KEYS),
        "shared_fields": Map(SHARED_FIELDS_KEYS),
        "cogs": MapPattern(Str(), Map(COG_KEYS)),
    }
)
SKIP_COG_KEYS_INFO_JSON = {"class_docstring"}
# TODO: auto-format to proper key order
AUTOLINT_REPO_KEYS_ORDER = list(REPO_KEYS.keys())
AUTOLINT_SHARED_FIELDS_KEYS_ORDER = [
    getattr(key, "key", key) for key in SHARED_FIELDS_KEYS
]

AUTOLINT_COG_KEYS_ORDER = [getattr(key, "key", key) for key in COG_KEYS]


def check_order(data: dict) -> int:
    """Temporary order checking, until strictyaml adds proper support for sorting."""
    to_check = {
        "repo": AUTOLINT_REPO_KEYS_ORDER,
        "shared_fields": AUTOLINT_SHARED_FIELDS_KEYS_ORDER,
    }
    exit_code = 0
    for key, order in to_check.items():
        section = data[key]
        original_keys = list(section.keys())
        sorted_keys = sorted(section.keys(), key=order.index)
        if original_keys != sorted_keys:
            print(
                "\033[93m\033[1mWARNING:\033[0m "
                f"Keys in `{key}` section have wrong order - use this order: "
                f"{', '.join(sorted_keys)}"
            )
            exit_code = 1

    original_cog_names = list(data["cogs"].keys())
    sorted_cog_names = sorted(data["cogs"].keys())
    if original_cog_names != sorted_cog_names:
        print(
            "\033[93m\033[1mWARNING:\033[0m "
            "Cog names in `cogs` section aren't sorted. Use alphabetical order."
        )
        exit_code = 1

    for pkg_name, cog_info in data["cogs"].items():
        # strictyaml breaks ordering of keys for some reason
        original_keys = [k for k, v in cog_info.items() if v]
        sorted_keys = sorted(
            (k for k, v in cog_info.items() if v), key=AUTOLINT_COG_KEYS_ORDER.index
        )
        if original_keys != sorted_keys:
            print(
                "\033[93m\033[1mWARNING:\033[0m "
                f"Keys in `cogs->{pkg_name}` section have wrong order"
                f" - use this order: {', '.join(sorted_keys)}"
            )
            print(original_keys)
            print(sorted_keys)
            exit_code = 1
        for key in ("required_cogs", "requirements", "tags"):
            list_or_dict = cog_info[key]
            if hasattr(list_or_dict, "keys"):
                original_list = list(list_or_dict.keys())
            else:
                original_list = list_or_dict
            sorted_list = sorted(original_list)
            if original_list != sorted_list:
                friendly_name = key.capitalize().replace("_", " ")
                print(
                    "\033[93m\033[1mWARNING:\033[0m "
                    f"{friendly_name} for `{pkg_name}` cog aren't sorted."
                    " Use alphabetical order."
                )
                print(original_list)
                print(sorted_list)
                exit_code = 1

    return exit_code


def update_class_docstrings(cogs: dict, repo_info: dict) -> int:
    """Update class docstrings with descriptions from info.yaml

    This is created with few assumptions:
    - name of cog's class is under "name" key in `cogs` dictionary
    - following imports until we find class definition is enough to find it
      - class name is imported directly:
      `from .rlstats import RLStats` not `from . import rlstats`
      - import is relative
      - star imports are ignored
    """
    for pkg_name, cog_info in cogs.items():
        class_name = cog_info["name"]
        path = ROOT_PATH / pkg_name / "__init__.py"
        if not path.is_file():
            raise RuntimeError("Folder `{pkg_name}` isn't a valid package.")
        while True:
            with path.open(encoding="utf-8") as fp:
                source = fp.read()
                tree = parso.parse(source)
            class_node = next(
                (node for node in tree.iter_classdefs() if node.name.value == class_name),
                None,
            )
            if class_node is not None:
                break

            for import_node in tree.iter_imports():
                if import_node.is_star_import():
                    # we're ignoring star imports
                    continue
                for import_path in import_node.get_paths():
                    if import_path[-1].value == class_name:
                        break
                else:
                    continue

                if import_node.level == 0:
                    raise RuntimeError("Script expected relative import of cog's class.")
                if import_node.level > 1:
                    raise RuntimeError("Attempted relative import beyond top-level package.")
                path = ROOT_PATH / pkg_name
                for part in import_path[:-1]:
                    path /= part.value
                path = path.with_suffix(".py")
                if not path.is_file():
                    raise RuntimeError(
                        f"Path `{path}` isn't a valid file. Finding cog's class failed."
                    )
                break

        doc_node = class_node.get_doc_node()
        new_docstring = cog_info.get("class_docstring") or cog_info["short"]
        replacements = {
            "repo_name": repo_info["name"],
            "cog_name": cog_info["name"],
        }
        new_docstring = new_docstring.format_map(replacements)
        if doc_node is not None:
            doc_node.value = f'"""{new_docstring}"""'
        else:
            first_leaf = class_node.children[-1].get_first_leaf()
            # gosh, this is horrible
            first_leaf.prefix = f'\n    """{new_docstring}"""\n'

        new_code = tree.get_code()
        if source != new_code:
            print(f"Updated class docstring for {class_name}")
            with path.open("w", encoding="utf-8") as fp:
                fp.write(tree.get_code())

    return 0


def check_cog_data_path_use(cogs: dict) -> int:
    for pkg_name in cogs:
        p = subprocess.run(
            ("git", "grep", "-q", "cog_data_path", "--", f"{pkg_name}/"),
            cwd=ROOT_PATH,
            check=False,
        )
        if p.returncode == 0:
            print(
                "\033[94m\033[1mINFO:\033[0m "
                f"{pkg_name} uses cog_data_path, make sure"
                " that you notify the user about it in install message."
            )
        elif p.returncode != 1:
            raise RuntimeError("git grep command failed")
    return 0


CONTAINERS = parso.python.tree._FUNC_CONTAINERS | {
    "async_funcdef",
    "funcdef",
    "classdef",
}

SMALL_STMT_LIST = {
    "expr_stmt",
    "del_stmt",
    "pass_stmt",
    "flow_stmt",
    "import_stmt",
    "global_stmt",
    "nonlocal_stmt",
    "assert_stmt",
}
CONTAINERS_WITHOUT_LOCAL_SCOPE = (
    parso.python.tree._RETURN_STMT_CONTAINERS
    | {"with_item"}
    | parso.python.tree._IMPORTS
    | SMALL_STMT_LIST
)


def _scan_recursively(
    children: typing.List[parso.tree.NodeOrLeaf], name: str, containers: typing.Set[str]
):
    for element in children:
        if element.type == name:
            yield element
        if element.type in containers:
            yield from _scan_recursively(element.children, name, containers)


def check_command_docstrings(cogs: dict) -> int:
    ret = 0
    for pkg_name in cogs:
        pkg_folder = ROOT_PATH / pkg_name
        for file in pkg_folder.glob("**/*.py"):
            with file.open() as fp:
                tree = parso.parse(fp.read())
            for node in _scan_recursively(tree.children, "async_funcdef", CONTAINERS):
                funcdef = node.children[-1]
                decorators = funcdef.get_decorators()
                ignore = False
                # DEP-WARN: use of private method
                for prefix_part in decorators[0].children[0]._split_prefix():
                    if (
                        prefix_part.type == "comment"
                        and prefix_part.value == "# geninfo-ignore: missing-docstring"
                    ):
                        ignore = True
                for deco in decorators:
                    maybe_name = deco.children[1]
                    if maybe_name.type == "dotted_name":
                        it = (n.value for n in maybe_name.children)
                        # ignore first item (can either be `commands` or `groupname`)
                        next(it, None)
                        deco_name = "".join(it)
                    elif maybe_name.type == "name":
                        deco_name = maybe_name.value
                    else:
                        raise RuntimeError("Unexpected type of decorator name.")
                    if deco_name in {".command", ".group"}:
                        break
                else:
                    continue
                if funcdef.get_doc_node() is None:
                    if not ignore:
                        print(
                            "\033[93m\033[1mWARNING:\033[0m "
                            f"command `{funcdef.name.value}` misses help docstring!"
                        )
                        ret = 1
                elif ignore:
                    print(
                        "\033[93m\033[1mWARNING:\033[0m "
                        f"command `{funcdef.name.value}` has unused"
                        " missing-docstring ignore comment!"
                    )
                    ret = 1
    return ret


def check_for_end_user_data_statement(cogs: dict) -> int:
    ret = 0
    for pkg_name, cog_info in cogs.items():
        path = ROOT_PATH / pkg_name / "__init__.py"
        if not path.is_file():
            raise RuntimeError("Folder `{pkg_name}` isn't a valid package.")
        with path.open(encoding="utf-8") as fp:
            source = fp.read()
        tree = parso.parse(source)
        for node in _scan_recursively(tree.children, "name", CONTAINERS_WITHOUT_LOCAL_SCOPE):
            if node.value == "__red_end_user_data_statement__":
                break
        else:
            print(
                "\033[93m\033[1mWARNING:\033[0m "
                f"cog package `{pkg_name}` is missing end user data statement!"
            )
            ret = 1

    return ret


MAX_RED_VERSIONS = {
    (3, 8): None,
}
MAX_PYTHON_VERSION = next(reversed(MAX_RED_VERSIONS))


def main() -> int:
    print("Loading info.yaml...")
    with open(ROOT_PATH / "info.yaml", encoding="utf-8") as fp:
        data = yaml_load(fp.read(), SCHEMA).data

    print("Checking order in sections...")
    exit_code = check_order(data)

    print("Preparing repo's info.json...")
    repo_info = data["repo"]
    repo_info["install_msg"] = repo_info["install_msg"].format_map(
        {"repo_name": repo_info["name"]}
    )
    with open(ROOT_PATH / "info.json", "w", encoding="utf-8") as fp:
        json.dump(repo_info, fp, indent=4)

    all_requirements: typing.Set[str] = set()
    requirements: typing.Dict[typing.Tuple[int, int], typing.Set[str]] = {
        (3, 8): set(),
    }
    black_file_list: typing.Dict[typing.Tuple[int, int], typing.List[str]] = {
        (3, 8): [".ci"],
    }
    compileall_file_list: typing.Dict[typing.Tuple[int, int], typing.List[str]] = {
        (3, 8): ["."],
    }
    print("Preparing info.json files for cogs...")
    shared_fields = data["shared_fields"]
    global_min_bot_version = shared_fields.get("min_bot_version")
    global_min_python_version = shared_fields.get("min_python_version")
    cogs = data["cogs"]
    for pkg_name, cog_info in cogs.items():
        all_requirements.update(cog_info["requirements"])
        min_bot_version = cog_info.get("min_bot_version", global_min_bot_version)
        min_python_version = (3, 8)
        if min_bot_version is not None:
            red_version_info = VersionInfo.from_str(min_bot_version)
            for python_version, max_red_version in MAX_RED_VERSIONS.items():
                if max_red_version is None:
                    min_python_version = python_version
                    break
                if red_version_info >= max_red_version:
                    continue
                min_python_version = python_version
                break
        python_version = cog_info.get("min_python_version", global_min_python_version)
        if python_version is not None and min_python_version < python_version:
            min_python_version = python_version
        for python_version, reqs in requirements.items():
            if python_version >= min_python_version:
                reqs.update(cog_info["requirements"])
        for python_version, file_list in compileall_file_list.items():
            if python_version is MAX_PYTHON_VERSION:
                continue
            if python_version >= min_python_version:
                file_list.append(pkg_name)
        black_file_list[min_python_version].append(pkg_name)

        print(f"Preparing info.json for {pkg_name} cog...")
        output = {}
        for key in AUTOLINT_COG_KEYS_ORDER:
            if key in SKIP_COG_KEYS_INFO_JSON:
                continue
            value = cog_info.get(key)
            if value is None:
                value = shared_fields.get(key)
            if value is None:
                continue
            output[key] = value
        replacements = {
            "repo_name": repo_info["name"],
            "cog_name": output["name"],
        }
        shared_fields_namespace = SimpleNamespace(**shared_fields)
        maybe_bundled_data = ROOT_PATH / pkg_name / "data"
        if maybe_bundled_data.is_dir():
            new_msg = f"{output['install_msg']}\n\nThis cog comes with bundled data."
            output["install_msg"] = new_msg
        for to_replace in ("short", "description", "install_msg"):
            output[to_replace] = safe_format_alt(
                output[to_replace], {"shared_fields": shared_fields_namespace}
            )
            if to_replace == "description":
                output[to_replace] = output[to_replace].format_map(
                    {**replacements, "short": output["short"]}
                )
            else:
                output[to_replace] = output[to_replace].format_map(replacements)

        with open(ROOT_PATH / pkg_name / "info.json", "w", encoding="utf-8") as fp:
            json.dump(output, fp, indent=4)

    print("Preparing requirements file for CI...")
    with open(ROOT_PATH / ".ci/requirements/all_cogs.txt", "w", encoding="utf-8") as fp:
        fp.write("Red-DiscordBot\n")
        for requirement in sorted(all_requirements):
            fp.write(f"{requirement}\n")
    for python_version, reqs in requirements.items():
        folder_name = f"py{''.join(map(str, python_version))}"
        with open(
            ROOT_PATH / f".ci/{folder_name}/requirements/all_cogs.txt",
            "w",
            encoding="utf-8",
        ) as fp:
            fp.write("Red-DiscordBot\n")
            for req in sorted(reqs):
                fp.write(f"{req}\n")
        with open(
            ROOT_PATH / f".ci/{folder_name}/black_file_list.txt", "w", encoding="utf-8"
        ) as fp:
            fp.write(" ".join(sorted(black_file_list[python_version])))
        with open(
            ROOT_PATH / f".ci/{folder_name}/compileall_file_list.txt",
            "w",
            encoding="utf-8",
        ) as fp:
            fp.write(" ".join(sorted(compileall_file_list[python_version])))

    print("Preparing all cogs list in README.md...")
    with open(ROOT_PATH / "README.md", "r+", encoding="utf-8") as fp:
        text = fp.read()
        match = re.search(r"## Cog Menu\n{2}(.+)\n{2}## Contributing", text, flags=re.DOTALL)
        if match is None:
            print("\033[91m\033[1mERROR:\033[0m Couldn't find cogs sections in README.md!")
            return 1
        start, end = match.span(1)
        lines = ["---\n| Name | Description |\n| --- | --- |"]
        for pkg_name, cog_info in cogs.items():
            replacements = {
                "repo_name": repo_info["name"],
                "cog_name": cog_info["name"],
            }
            desc = cog_info["short"].format_map(replacements)
            lines.append(f"| {pkg_name} | {desc} |")
        cogs_section = "\n".join(lines)
        fp.seek(0)
        fp.truncate()
        fp.write(f"{text[:start]}{cogs_section}{text[end:]}")

    print("Updating class docstrings...")
    update_class_docstrings(cogs, repo_info)
    print("Checking for cog_data_path usage...")
    check_cog_data_path_use(cogs)
    print("Checking for missing help docstrings...")
    check_command_docstrings(cogs)
    print("Checking for missing end user data statements...")
    check_for_end_user_data_statement(cogs)

    print("Done!")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
