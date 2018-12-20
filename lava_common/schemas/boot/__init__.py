# -*- coding: utf-8 -*-
#
# Copyright (C) 2018 Linaro Limited
#
# Author: Rémi Duraffort <remi.duraffort@linaro.org>
#
# This file is part of LAVA.
#
# LAVA is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

from voluptuous import All, Any, Length, Required, Optional, Range

from lava_common.schemas import action


def auto_login():
    return Any(
        {
            Required("login_prompt"): All(str, Length(min=1)),
            Required("username"): str,
            Optional("login_commands"): [All(str, Length(min=1))],
        },
        {
            Required("login_prompt"): All(str, Length(min=1)),
            Required("username"): str,
            Required("password_prompt"): str,
            Required("password"): str,
            Optional("login_commands"): [All(str, Length(min=1))],
        },
    )


def transfer_overlay():
    return {Required("download_command"): str, Required("unpack_command"): str}


def prompts():
    # FIXME: prompts should only accept a list
    return Any(All(str, Length(min=1)), [All(str, Length(min=1))])


def schema(live=False):
    base = {
        **action(live),
        Optional("repeat"): Range(min=1),  # TODO: where to put it?
        Optional("failure_retry"): Range(min=1),  # TODO: where to put it?
        Optional("parameters"): {
            Optional("kernel-start-message"): str,
            Optional("shutdown-message"): str,
        },
    }
    return base
