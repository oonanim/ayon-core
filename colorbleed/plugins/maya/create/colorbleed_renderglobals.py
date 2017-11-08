from collections import OrderedDict

import maya.cmds as cmds

import avalon.maya


class CreateRenderGlobals(avalon.maya.Creator):

    label = "Render Globals"
    family = "colorbleed.renderglobals"
    icon = "gears"

    def __init__(self, *args, **kwargs):
        super(CreateRenderGlobals, self).__init__(*args, **kwargs)

        # We won't be publishing this one
        self.data["id"] = "avalon.renderglobals"

        # We don't need subset or asset attributes
        self.data.pop("subset", None)
        self.data.pop("asset", None)

        data = OrderedDict(**self.data)

        startframe = cmds.playbackOptions(query=True, animationStartTime=True)
        endframe = cmds.playbackOptions(query=True, animationEndTime=True)

        data["suspendPublishJob"] = False
        data["includeDefaultRenderLayer"] = False
        data["priority"] = 50
        data["whitelist"] = False
        data["machineList"] = ""
        data["startFrame"] = int(startframe)
        data["endFrame"] = int(endframe)

        self.data = data
        self.options = {"useSelection": False}  # Force no content

    def process(self):

        exists = cmds.ls(self.name)
        assert len(exists) <= 1, (
            "More than one renderglobal exists, this is a bug")

        if exists:
            return cmds.warning("%s already exists." % exists[0])

        super(CreateRenderGlobals, self).process()
