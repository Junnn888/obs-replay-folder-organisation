**Forked from [oxypatic/RecORDER](https://github.com/oxypatic/RecORDER)** by oxypatic (formerly padiix). This repository is a reliability-focused fork of that script, released under the same AGPL-3.0 license. All credit for the original idea, design and code goes to the upstream author; see [Origin and credits](#origin-and-credits).

![Logo](https://github.com/user-attachments/assets/273b1b70-aa5a-43c3-a669-2cf8704adf18)


<div align="right">
   <picture> 
      <img src="https://img.shields.io/badge/version-3.2.0-11">
   </picture>
   <picture> 
      <img src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff">
   </picture>
   <picture>
      <img src="https://img.shields.io/badge/Linux-FCC624?logo=linux&logoColor=black">
   </picture>
   <picture>
      <img src="https://img.shields.io/badge/macOS-000000?logo=apple&logoColor=F0F0F0">
   </picture>
   <picture>
      <img src="https://custom-icon-badges.demolab.com/badge/Windows-0078D6?logo=windows11&logoColor=white">
   </picture>
</div>

## Table of Content
* [What does this script does?](#what-does-this-script-do)
* [Features of the script](#features-of-the-script)
* [What do I need to do to make it work?](#what-do-i-need-to-do-to-make-it-work)
* [Known limitations](#known-limitations)
* [FAQ](#faq)
* [Origin and credits](#origin-and-credits)

## What does this script do?
The script recreates the organization of NVIDIA Shadow Play, <br>which placed all media captured while playing specific game into folder called after the game.

## Requirements
> [!NOTE]  
> This script is designed for ease of use, but the game detection itself depends on your Operating System - see [Known limitations](#known-limitations)

* Script only works with OBS in version **29.1.0** or higher (it needs source UUIDs)
* Script requires only a **Python 3.10 version** or higher
> [!IMPORTANT]
> (**3.12** is the highest the OBS 31.x supports for now)
   * No need for tkinter or anything additionally, minimal python works

## Features of the script
### Main behaviour:
- __Organizes recordings__ in folders called after captured Game/Window
- __Reacts to splitting of recordings__ and actively moves all the splits to relevant folder
- __Remembers the last game for a while__, so a clip saved right after the game closed still lands in the right folder
   - The memory survives switching scenes, but is cleared when you switch _scene collections_ (the old collection's sources are gone)
- __Never touches your recording__ - RecORDER only moves files after OBS is done writing them

### Customizable features:
- __Fallback folder name__ (_the folder to which media will be organized if it cannot find window title_)
- __Replay folder name__ and __Screenshot folder name__ (_the subfolder inside the game folder_)
- __Name folders by__ window title or executable name
- __Remember last game for (s)__ (_grace window after the game loses the hook_)
- __Organization mode__ (_decide how you want your media organized_)
- __Organization of Replay Buffer recordings__ by RecORDER
- __Organization of screenshots__ by RecORDER

### Other features:
- __Verbose logs of the script__ 
   - View important debug information when checking `Script Logs`
- __Check for updates button__
   - Quickly check if RecORDER have any new updates for you!


## What do I need to do to make it work?
First things first!
1. Install Python - a version [3.10](https://www.python.org/downloads/release/python-31011/) will work, but you can use newer one - [3.12](https://www.python.org/downloads/release/python-31212/).
   > Version 3.12 will give you the best compatibility
2. Next - configure the Python - located under `Tools > Scripts > Python Settings` inside OBS.
   > Select the root folder the Python resides<br>
   > _Default Python folder name_: `Python312`
3. Half way there! <br>Next you need to add the script in the `Tools > Scripts`
   > Click the "+" button and select the `RecORDERvX-X.py` script.
   > 
   > For ease of use, place the script in OBS installation folder, <br>the relative path: `obs-studio\data\obs-plugins\frontend-tools\scripts`
4. Configure the script in a way you see fit
   > Explanation of the settings:
   > - __Monitored source__:
   >     - Source that is capturing the video from Game/Window (Game Capture or Window Capture)
   >     - `(Auto-detect)` searches the current scene - including groups and nested scenes - and picks the source that is currently hooked, preferring Game Capture
   >     - _Default_: (Auto-detect)
   > - __Organization mode__:
   >     - How should the script organize your recordings
   >     - Currently available settings:
   >        - __Basic__ - sorts media __based on Game/Window title__ and __media type__ (_Recording/Replay Buffer/Screenshot_)
   >        - __Group by Date__  - _Basic_ and also organizes media into __folders created with recording's creation date__
   >     - _Default_: Basic
   > - __Fallback folder name__:
   >     - Folder name for recordings that couldn't be organized based on the window title.
   >     - _Default_: Any Recording
   > - __Replay folder name__:
   >     - Name of the subfolder replays go into, ex. `<recordings>\ELDEN RING\replay\`
   >     - _Default_: replay
   > - __Screenshot folder name__:
   >     - Name of the subfolder screenshots go into, ex. `<recordings>\ELDEN RING\screenshot\`
   >     - _Default_: screenshot
   > - __Name folders by__:
   >     - `Window title` gives you ex. _ELDEN RING_, `Executable name` gives you ex. _eldenring_
   >     - The other one is used automatically when the preferred one is empty or useless (ex. _Untitled_)
   >     - _Default_: Window title
   > - __Remember last game for (s)__:
   >     - If the game closed (or lost the hook) right before you saved the clip, RecORDER keeps using its folder for this many seconds
   >     - Set to `0` to disable and always fall back to the fallback folder
   >     - _Default_: 120
   > - __Organize Replay Buffer recordings__  
   >     - Check it, if you want your Replay Buffer files to be organized by RecORDER
   >     - _Default_: Enabled
   > - __Organize screenshots__  
   >     - Check it, if you want your screenshot files to be organized by RecORDER
   >     - _Default_: Enabled
   > - __Add game name as a file prefix__  
   >     - Check it, if you want your recordings to look like this:
   >        - ex. _Voices of The Void - %Filename Formatting%.mp4_
   >        - Filename Formatting is configured in `Settings > Advanced > Recording`
   >     - _Default_: Disabled
   > - __Verbose logging__  
   >     - Writes extra debug lines to the `Script Log`. Handy when reporting an issue.
   >     - _Default_: Disabled


## Known limitations

* __Windows is the only fully supported system.__ RecORDER asks the capture source which window it is hooked to, and only Windows' Game Capture and Window Capture answer that question.
   * __Linux__: works only with the X11 window capture (`xcomposite_input`). Wayland/PipeWire capture cannot report a hooked window.
   * __macOS__: OBS' macOS capture sources have no hook concept at all, so every file goes to the fallback folder.
* __Group by Date uses the time the file was saved__, not the moment the clip started. For a replay buffer clip saved just after midnight the folder is the new day.
* __Auto-remux handling is best effort.__ When the profile has _Automatically remux to mp4_ enabled, RecORDER waits (up to 10 minutes) for the `.mp4` to appear next to the original recording and then moves both files. If the remux takes longer than that, only the original file is moved and the remuxed one stays in the recording folder.
* __Screenshots and replays are named after the game hooked at the moment they are saved__, which is what you want in almost every case - but if you alt-tab out of the game for longer than the "Remember last game" window, the file lands in the fallback folder.
* RecORDER never starts, stops or saves anything on its own - if OBS did not write a file, there is nothing for the script to organize.

## FAQ

<details>
<summary>Work in Progress</summary>
</details>

## Origin and credits

This project is a fork of **[oxypatic/RecORDER](https://github.com/oxypatic/RecORDER)**, written and maintained by
[oxypatic](https://github.com/oxypatic) (the repository was previously published under the name padiix).
The logo, the ShadowPlay-style folder layout, the settings, the per-scene source memory and the split-recording
support all come from the upstream project.

What this fork adds is a reliability pass on top of upstream v3.1.1:

* an audit of the original script ([AUDIT.md](AUDIT.md)),
* a rewrite against those findings (`RecORDER.py`, version 3.2.0),
* a test suite with a stand-in `obspython` module (`tests/`),
* the unmodified upstream script for comparison (`reference/RecORDER_3.1.1_original.py`).

Both the original and this fork are licensed under the **GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)).
If you find the script useful, please star the upstream repository as well.
