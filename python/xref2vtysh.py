# SPDX-License-Identifier: GPL-2.0-or-later
# FRR xref vtysh command extraction
#
# Copyright (C) 2022  David Lamparter for NetDEF, Inc.

"""
Generate vtysh_cmd.c from frr .xref file(s).

This can run either standalone or as part of xrelfo.  The latter saves a
non-negligible amount of time (0.5s on average systems, more on e.g. slow ARMs)
since serializing and deserializing JSON is a significant bottleneck in this.
"""

import sys
import os
import re
import pathlib
import argparse
from collections import defaultdict
import difflib

import json

try:
    import ujson as json  # type: ignore
except ImportError:
    pass

frr_top_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# vtysh needs to know which daemon(s) to send commands to.  For lib/, this is
# not quite obvious...

daemon_flags = {
    "lib/agentx.c": "VTYSH_ISISD|VTYSH_RIPD|VTYSH_OSPFD|VTYSH_OSPF6D|VTYSH_BGPD|VTYSH_ZEBRA",
    "lib/filter.c": "VTYSH_ACL",
    "lib/filter_cli.c": "VTYSH_ACL",
    "lib/if.c": "VTYSH_INTERFACE",
    "lib/keychain.c": "VTYSH_RIPD|VTYSH_EIGRPD|VTYSH_OSPF6D",
    "lib/lib_vty.c": "VTYSH_ALL",
    "lib/log_vty.c": "VTYSH_ALL",
    "lib/nexthop_group.c": "VTYSH_NH_GROUP",
    "lib/resolver.c": "VTYSH_NHRPD|VTYSH_BGPD",
    "lib/routemap.c": "VTYSH_RMAP",
    "lib/routemap_cli.c": "VTYSH_RMAP",
    "lib/spf_backoff.c": "VTYSH_ISISD",
    "lib/event.c": "VTYSH_ALL",
    "lib/vrf.c": "VTYSH_VRF",
    "lib/vty.c": "VTYSH_ALL",
}

vtysh_cmd_head = """/* autogenerated file, DO NOT EDIT! */
#include <zebra.h>

#include "command.h"
#include "linklist.h"

#include "vtysh/vtysh.h"
"""

if sys.stderr.isatty():
    _fmt_red = "\033[31m"
    _fmt_green = "\033[32m"
    _fmt_clear = "\033[m"
else:
    _fmt_red = _fmt_green = _fmt_clear = ""


def c_escape(text: str) -> str:
    """
    Escape string for output into C source code.

    Handles only what's needed here.  CLI strings and help text don't contain
    weird special characters.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class NodeDict(defaultdict):
    """
    CLI node ID (integer) -> dict of commands in that node.
    """

    nodenames = {}  # Dict[int, str]

    def __init__(self):
        super().__init__(dict)

    def items_named(self):
        for k, v in self.items():
            yield self.nodename(k), v

    @classmethod
    def nodename(cls, nodeid: int) -> str:
        return cls.nodenames.get(nodeid, str(nodeid))

    @classmethod
    def load_nodenames(cls):
        with open(os.path.join(frr_top_src, "lib", "command.h"), "r") as fd:
            command_h = fd.read()

        nodes = re.search(r"enum\s+node_type\s+\{(.*?)\}", command_h, re.S)
        if nodes is None:
            raise RuntimeError(
                "regex failed to match on lib/command.h (to get CLI node names)"
            )

        text = nodes.group(1)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//.*?$", "", text, flags=re.M)
        text = text.replace(",", " ")
        text = text.split()

        for i, name in enumerate(text):
            cls.nodenames[i] = name


class CommandEntry:
    """
    CLI command definition.

    - one DEFUN creates at most one of these, even if the same command is
      installed in multiple CLI nodes (e.g. BGP address-family nodes)
    - for each CLI node, commands with the same CLI string are merged.  This
      is *almost* irrelevant - ospfd & ospf6d define some identical commands
      in the route-map node.  Those must be merged for things to work
      correctly.
    """

    all_defs = []  # List[CommandEntry]
    warn_counter = 0

    def __init__(self, origin, name, spec):
        self.origin = origin
        self.name = name
        self._spec = spec
        self._registered = False

        self.cmd = spec["string"]
        self._cmd_normalized = self.normalize_cmd(self.cmd)

        self.hidden = "hidden" in spec.get("attrs", [])
        self.daemons = self._get_daemons()

        self.doclines = self._spec["doc"].splitlines(keepends=True)
        if not self.doclines[-1].endswith("\n"):
            self.warn_loc("docstring does not end with \\n")

    def warn_loc(self, wtext, nodename=None):
        """
        Print warning with parseable (compiler style) location

        Matching the way compilers emit file/lineno means editors/IDE can
        identify / jump to the error location.
        """

        if nodename:
            prefix = ": [%s] %s:" % (nodename, self.name)
        else:
            prefix = ": %s:" % (self.name,)

        for line in wtext.rstrip("\n").split("\n"):
            sys.stderr.write(
                "%s:%d%s %s\n"
                % (
                    self._spec["defun"]["file"],
                    self._spec["defun"]["line"],
                    prefix,
                    line,
                )
            )
            prefix = "-    "

        CommandEntry.warn_counter += 1

    def _get_daemons(self):
        path = pathlib.Path(self.origin)
        if path.name == "vtysh":
            return {}

        defun_file = os.path.relpath(self._spec["defun"]["file"], frr_top_src)
        defun_path = pathlib.Path(defun_file)

        if defun_path.parts[0] != "lib":
            if "." not in path.name:
                # daemons don't have dots in their filename
                return {"VTYSH_" + path.name.upper()}

            # loadable modules - use directory name to determine daemon
            return {"VTYSH_" + path.parts[-2].upper()}

        if defun_file in daemon_flags:
            return {daemon_flags[defun_file]}

        v6_cmd = "ipv6" in self.name
        if defun_file == "lib/plist.c":
            if v6_cmd:
                return {
                    "VTYSH_RIPNGD|VTYSH_OSPF6D|VTYSH_BGPD|VTYSH_ZEBRA|VTYSH_PIM6D|VTYSH_BABELD|VTYSH_ISISD|VTYSH_FABRICD"
                }
            else:
                return {
                    "VTYSH_RIPD|VTYSH_OSPFD|VTYSH_BGPD|VTYSH_ZEBRA|VTYSH_PIMD|VTYSH_EIGRPD|VTYSH_BABELD|VTYSH_ISISD|VTYSH_FABRICD"
                }

        if defun_file == "lib/if_rmap.c":
            if v6_cmd:
                return {"VTYSH_RIPNGD"}
            else:
                return {"VTYSH_RIPD"}

        return {}

    def __repr__(self):
        return "<CommandEntry %s: %r>" % (self.name, self.cmd)

    def register(self):
        """Track DEFUNs so each is only output once."""
        if not self._registered:
            self.all_defs.append(self)
            self._registered = True
        return self

    def merge(self, other, nodename):
        if self._cmd_normalized != other._cmd_normalized:
            self.warn_loc(
                "command definition mismatch, first definied as:\n%r" % (self.cmd,),
                nodename=nodename,
            )
            other.warn_loc("later defined as:\n%r" % (other.cmd,), nodename=nodename)

        if self._spec["doc"] != other._spec["doc"]:
            self.warn_loc(
                "help string mismatch, first defined here (-)", nodename=nodename
            )
            other.warn_loc(
                "later defined here (+)\nnote: both commands define %r in same node (%s)"
                % (self.cmd, nodename),
                nodename=nodename,
            )

            d = difflib.Differ()
            for diffline in d.compare(self.doclines, other.doclines):
                if diffline.startswith("  "):
                    continue
                if diffline.startswith("+ "):
                    diffline = _fmt_green + diffline
                elif diffline.startswith("- "):
                    diffline = _fmt_red + diffline
                sys.stderr.write("\t" + diffline.rstrip("\n") + _fmt_clear + "\n")

        if self.hidden != other.hidden:
            self.warn_loc(
                "hidden flag mismatch, first %r here" % (self.hidden,),
                nodename=nodename,
            )
            other.warn_loc(
                "later %r here (+)\nnote: both commands define %r in same node (%s)"
                % (other.hidden, self.cmd, nodename),
                nodename=nodename,
            )

        # ensure name is deterministic regardless of input DEFUN order
        self.name = min([self.name, other.name], key=lambda i: (len(i), i))
        self.daemons.update(other.daemons)

    def get_def(self):
        doc = "\n".join(['\t"%s"' % c_escape(line) for line in self.doclines])
        defsh = "DEFSH_HIDDEN" if self.hidden else "DEFSH"

        # make daemon list deterministic
        daemons = set()
        for daemon in self.daemons:
            daemons.update(daemon.split("|"))
        daemon_str = "|".join(sorted(daemons))

        return """
%s (%s, %s_vtysh,
\t"%s",
%s)
""" % (
            defsh,
            daemon_str,
            self.name,
            c_escape(self.cmd),
            doc,
        )

    # accept slightly different command definitions that result in the same command
    re_collapse_ws = re.compile(r"\s+")
    re_remove_varnames = re.compile(r"\$[a-z][a-z0-9_]*")

    @classmethod
    def normalize_cmd(cls, cmd):
        cmd = cmd.strip()
        cmd = cls.re_collapse_ws.sub(" ", cmd)
        cmd = cls.re_remove_varnames.sub("", cmd)
        return cmd

    @classmethod
    def process(cls, nodes, name, origin, spec):
        if "nosh" in spec.get("attrs", []):
            return
        if origin == "vtysh/vtysh":
            return

        if origin == "isisd/fabricd":
            # dirty workaround :(
            name = "fabricd_" + name

        entry = cls(origin, name, spec)
        if not entry.daemons:
            return

        for nodedata in spec.get("nodes", []):
            node = nodes[nodedata["node"]]
            if entry._cmd_normalized not in node:
                node[entry._cmd_normalized] = entry.register()
            else:
                node[entry._cmd_normalized].merge(
                    entry, nodes.nodename(nodedata["node"])
                )

    @classmethod
    def load(cls, xref):
        nodes = NodeDict()

        mgmtname = "mgmtd/libmgmt_be_nb.la"
        for cmd_name, origins in xref.get("cli", {}).items():
            # If mgmtd has a yang version of a CLI command, make it the only daemon
            # to handle it.  For now, daemons can still be compiling their cmds into the
            # binaries to allow for running standalone with CLI config files. When they
            # do this they will also be present in the xref file, but we want to ignore
            # those in vtysh.
            if "yang" in origins.get(mgmtname, {}).get("attrs", []):
                CommandEntry.process(nodes, cmd_name, mgmtname, origins[mgmtname])
                continue

            for origin, spec in origins.items():
                CommandEntry.process(nodes, cmd_name, origin, spec)
        return nodes

    @classmethod
    def output_defs(cls, ofd):
        for entry in sorted(cls.all_defs, key=lambda i: i.name):
            ofd.write(entry.get_def())

    @classmethod
    def output_install(cls, ofd, nodes):
        ofd.write("\nvoid vtysh_init_cmd(void)\n{\n")

        for name, items in sorted(nodes.items_named()):
            for item in sorted(items.values(), key=lambda i: i.name):
                ofd.write("\tinstall_element(%s, &%s_vtysh);\n" % (name, item.name))

        ofd.write("}\n")

    @classmethod
    def run(cls, xref, ofd):
        ofd.write(vtysh_cmd_head)

        NodeDict.load_nodenames()
        nodes = cls.load(xref)
        cls.output_defs(ofd)
        cls.output_install(ofd, nodes)


def main():
    argp = argparse.ArgumentParser(description="FRR xref to vtysh defs")
    argp.add_argument(
        "xreffile", metavar="XREFFILE", type=str, help=".xref file to read"
    )
    argp.add_argument("-Werror", action="store_const", const=True)
    args = argp.parse_args()

    with open(args.xreffile, "r") as fd:
        data = json.load(fd)

    CommandEntry.run(data, sys.stdout)

    if args.Werror and CommandEntry.warn_counter:
        sys.exit(1)


if __name__ == "__main__":
    main()
