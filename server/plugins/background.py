import asyncio
import datetime
import sys
import time

import plugins.basetypes
import plugins.configuration
import plugins.offloader
import asyncio
import os
import queue
import threading
import re
import time
import yaml
import fnmatch
import typing

try:
    loader = typing.Union[yaml.Loader, yaml.CLoader]  # mypy fixups
    dumper = typing.Union[yaml.Dumper, yaml.CDumper]  # mypy fixups
    from yaml import CLoader as loader, CDumper as dumper

    print("Using fast C++ YAML parser..!")
except:
    from yaml import Loader as loader, Dumper as dumper

LOCK = threading.Lock()
MERGE_ISSUES = False


def process_files(tid, server, files: queue.Queue, path, excludes, bad_words, bad_words_re, excludes_context):
    current_issues = []
    bad_words_stacked: dict = {}
    no_files = files.qsize()
    f_p = 0
    now = time.time()
    while files.qsize():
        try:
            LOCK.acquire(blocking=True)
            file = files.get(block=True, timeout=2)
            files_processed = no_files - files.qsize()
            pct = int(files_processed * 100 / no_files)
            duration = int(time.time() - now)
            server.data.activity = f"Scanning {path}. Currently {pct}% done ({files_processed} out of {no_files} files scanned, {duration} seconds spent)"
            LOCK.release()
        except:
            print("Thread broke or queue emptied, exiting...")
            try:
                LOCK.release()
            except RuntimeError:  # Can't unlock an unlocked lock
                pass
            break
        if os.path.islink(file):
            continue  # no symlinks, please
        if any(
            fnmatch.fnmatch(file, foo) or fnmatch.fnmatch(file.replace(path, "", 1).lstrip("/"), foo)
            for foo in excludes
        ):
            continue  # don't match excludes
        try:
            with open(file, encoding="utf-8") as f:
                f_p += 1
                line_no = 0
                for line in f:
                    line_no += 1
                    line_lowercase = line.lower()
                    for bad_word in bad_words:
                        if bad_word in line_lowercase:
                            bad_word_re = bad_words_re[bad_word]
                            word_no = 0
                            for word in bad_word_re.finditer(line_lowercase):
                                word_no += 1
                                matched_word = word.group(1)
                                ctx_start = max(0, word.start(1) - 64)
                                ctx_end = min(len(line), word.end(1) + 64)
                                try:
                                    if any(ctx and re.search(ctx, line_lowercase) for ctx in excludes_context):
                                        continue
                                except SyntaxError:  # Bad regex
                                    pass
                                if server.config.debug.print_issues:
                                    LOCK.acquire(blocking=True)
                                    print(f"#{tid}: Found potential issue in {file} on line {line_no}: {matched_word}")
                                    LOCK.release()
                                bad_words_stacked[matched_word] = bad_words_stacked.get(matched_word, 0) + 1
                                current_issues.append(
                                    {
                                        "path": file,
                                        "line": line_no,
                                        "mark": word_no,
                                        "word": matched_word,
                                        "reason": bad_words[matched_word],
                                        "context": line[ctx_start:ctx_end].strip(),
                                        "resolution": None,
                                    }
                                )
        except UnicodeDecodeError:
            pass  # Binary file
    return current_issues, bad_words_stacked, f_p


async def scan_project(server, project, path):
    """Scans a project repo, looking for potential wording issues"""
    if server.config.debug.print_issues:
        print(f"Starting scheduled scan of {path}...")
    git_exec = server.config.executables["git"]
    all_files = []
    yml = project.settings
    bad_words = server.config.words
    if "bad_words" in yml:
        bad_words = yml["bad_words"]
    excludes = server.config.excludes
    if "excludes" in yml:
        excludes = yml["excludes"]

    excludes_context = []
    if "excludes_context" in yml:
        excludes_context = yml["excludes_context"]

    scan_history = project.history
    server.data.activity = f"Preparing to scan {path}..."

    params = (
        "-C",
        path,
        "stash",
    )
    proc = await asyncio.create_subprocess_exec(
        git_exec, *params, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    params = (
        "-C",
        path,
        "pull",
    )
    proc = await asyncio.create_subprocess_exec(
        git_exec, *params, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        for root_path, directories, files in os.walk(path):
            if "/.git" in root_path:
                continue
            # Grab all files except those starting with _clc, which is our yaml files.
            all_files.extend([os.path.join(root_path, file) for file in files if not file.startswith("_clc")])
        files_processed = 0
        bytes_processed = 0
        problems_found = 0
        current_issues = []

        # Precompile all bad words
        bad_words_stacked = {}
        bad_words_re = {}
        for word in bad_words:
            # If large word, find it anywhere
            if len(word) > 5:
                bad_words_re[word] = re.compile(r"(" + word + r")", flags=re.UNICODE)
            # If smaller word, require boundaries but allow for endings
            else:
                bad_words_re[word] = re.compile(r"(?:\b|\W|_)+(" + word + r")(?:ed|ing|s)?(?:\b|\W|_)+", flags=re.UNICODE)
            bad_words_stacked[word] = 0

        # How many files and when did we start scanning
        now = time.time()

        runners = plugins.offloader.ExecutorPool(threads=10)

        awaits = []
        all_files_thrd = queue.Queue()
        for file in all_files:
            all_files_thrd.put_nowait(file)

        for i in range(0, 4):
            awaits.append(
                asyncio.create_task(
                    runners.run(
                        process_files,
                        i,
                        server,
                        all_files_thrd,
                        path,
                        excludes,
                        bad_words,
                        bad_words_re,
                        excludes_context,
                    )
                )
            )

        for x in awaits:
            problems_tmp, bad_words_tmp, f_p = await x
            files_processed += f_p
            if problems_tmp:
                current_issues.extend(problems_tmp)
                problems_found += len(problems_tmp)
            if bad_words_tmp:
                for k, v in bad_words_tmp.items():
                    bad_words_stacked[k] += v

        taken = time.time() - now
        if server.config.debug.print_issues:
            print(
                f"Processed {path} in {int(taken)} seconds, found {problems_found} potential issues in {files_processed} text files."
            )
        yml["lastrun"] = int(time.time())
        yml["scans"] += 1
        yml["status"] = {
            "files_processed": files_processed,
            "bytes_processed": bytes_processed,
            "issues": problems_found,
            "duration": taken,
            "epoch": int(now),
            "words_stacked": bad_words_stacked,
        }

        scan_history.append(yml["status"])

        # Compile current issues, merging in old ones
        clc_issues = []
        clc_issues_file = os.path.join(path, "_clc_issues.yaml")
        if MERGE_ISSUES:
            if os.path.exists(clc_issues_file):
                clc_issues = yaml.safe_load(open(clc_issues_file))
            for issue in current_issues:
                for old_issue in clc_issues:
                    if old_issue["path"] == issue["path"]:
                        if old_issue["line"] in ("*", issue["line"]) and old_issue["word"] == issue["word"]:
                            issue["resolution"] = old_issue["resolution"]
                            issue["line"] = old_issue["line"]
                            issue["word"] = old_issue["word"]
                            if issue["resolution"] == "ignore":
                                problems_found -= 1

        # Save updated settings
        project.settings = yml
        ymlpath = os.path.join(path, "_clc.yaml")
        yaml.dump(yml, open(ymlpath, "w"))
        project.mtimes[ymlpath] = os.stat(ymlpath).st_mtime

        server.data.activity = f"Writing report for last scan of {path}...could take a while."

        # Save scan history
        project.history = scan_history
        history_file = os.path.join(path, "_clc_history.yaml")
        yaml.dump(scan_history, open(history_file, "w"))
        project.mtimes[history_file] = os.stat(history_file).st_mtime

        # Writing issues could take AGES, so we offload to a thread
        if server.config.debug.print_issues:
            print("Writing issue YAML...")
        current_issues = sorted(current_issues, key=lambda x: x["path"])
        clc_issues_file_tmp = clc_issues_file + ".tmp"
        await runners.run(yaml.dump, current_issues, open(clc_issues_file_tmp, "w"), Dumper=dumper)
        if os.path.exists(clc_issues_file):
            os.unlink(clc_issues_file)
        os.rename(clc_issues_file_tmp, clc_issues_file)

        if server.config.debug.print_issues:
            print("Done, back to idling.")
    else:
        print(f"Could not pull in latest changes for {path}, ignoring for now...")
        print(stderr)
    server.data.activity = "Idling..."


async def run_tasks(server: plugins.basetypes.Server):
    """
        Runs long-lived background data gathering tasks such as gathering repositories, projects and ldap/mfa data.

        Generally runs every 2½ minutes, or whatever is set in tasks/refresh_rate in boxer.yaml
    """
    git_exec = server.config.executables["git"]
    await asyncio.sleep(3)
    while True:
        #  print("Running background tasks...")
        pqueue = server.data.project_queue[:]
        server.data.project_queue = []
        for item in pqueue:
            url = item["url"]
            branch = item["branch"]
            reponame = url.split("/")[-1]
            destination = os.path.join(server.config.dirs.scratch, reponame)
            params = ["clone", url, destination]
            if branch:
                params = ["clone", "-b", branch, url, destination]
            else:
                branch = "$default"
            print(f"Checking out {url} ({branch}) into {destination}")
            server.data.activity = f"Cloning repository into {destination}..."
            proc = await asyncio.create_subprocess_exec(
                git_exec, *params, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                yml = {
                    "source": url,
                    "branch": branch,
                    "excludes": item["excludes"],
                    "bad_words": item["words"],
                    "excludes_context": item["excludes_context"],
                    "checkout": int(time.time()),
                    "lastrun": 0,
                    "scans": 0,
                    "scan_history": [],
                }
                with open(os.path.join(destination, "_clc.yaml"), "w") as f:
                    yaml.dump(yml, f)
                    project = plugins.configuration.Project(reponame)
                    project.settings = yml
                    server.data.projects[reponame] = project
            print("Done!")
            server.data.activity = "Idling..."

        for repo in sorted(os.listdir(server.config.dirs.scratch)):
            path = os.path.join(server.config.dirs.scratch, repo)
            _clc_yaml_path = os.path.join(path, "_clc.yaml")
            _clc_yaml_history_path = os.path.join(path, "_clc_history.yaml")
            mtime = None
            if os.path.exists(_clc_yaml_path):
                mtime = os.stat(_clc_yaml_path)
            hmtime = None
            if os.path.exists(_clc_yaml_history_path):
                hmtime = os.stat(_clc_yaml_history_path)

            reload_files = False
            if repo not in server.data.projects:
                server.data.projects[repo] = plugins.configuration.Project(repo)
                reload_files = True
            else:
                if mtime and mtime.st_mtime != server.data.projects[repo].mtimes.get(_clc_yaml_path, 0):
                    reload_files = True
                if hmtime and hmtime.st_mtime != server.data.projects[repo].mtimes.get(_clc_yaml_history_path, 0):
                    reload_files = True

            if reload_files:
                print(f"{_clc_yaml_path} changed on disk, reloading.")
                if os.path.exists(_clc_yaml_path):
                    yml = yaml.safe_load(open(_clc_yaml_path))
                    server.data.projects[repo].settings = yml
                    if mtime:
                        server.data.projects[repo].mtimes[_clc_yaml_path] = mtime.st_mtime
                if os.path.exists(_clc_yaml_history_path):
                    yml = yaml.safe_load(open(_clc_yaml_history_path))
                    server.data.projects[repo].history = yml
                    if hmtime:
                        server.data.projects[repo].mtimes[_clc_yaml_history_path] = hmtime.st_mtime
            if mtime:
                yml = server.data.projects[repo].settings
                if "lastrun" in yml and yml["lastrun"] > time.time() - server.config.tasks.refresh_rate:
                    continue
            else:
                continue
            await scan_project(server, server.data.projects[repo], path)

        await asyncio.sleep(5)
