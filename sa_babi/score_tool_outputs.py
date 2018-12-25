import re
import yaml
import json
import csv
import logging
from pathlib import PurePath
from collections import defaultdict, namedtuple
#from generate import Tag
from sa_tag import Tag

Alert = namedtuple('Alert', ["tool", "checker", "file", "line", "message"])


def get_tag_for_alert(alert, defects):
    tags = defects.get(alert.file)
    if tags is not None:
        return Tag(tags[int(alert.line) - 1])
    else:
        return None


def correlate(alerts, defects):
    return ((a, get_tag_for_alert(a, defects)) for a in alerts)


def follows_rule(rule, s):
    return \
        (type(rule) is str and rule == s) \
     or (type(rule) is dict and ("regex" in rule and re.match(rule["regex"], s)))


def is_whitelisted(alert, whitelist):
    rules = whitelist.get(alert.tool) or {}
    checker_rules = rules.get("checkers") or []
    message_rules = rules.get("messages") or []
    return \
        any((follows_rule(rule, alert.checker) for rule in checker_rules)) \
     or any((follows_rule(rule, alert.message) for rule in message_rules))


def load_alerts(alerts_path, whitelist):
    result = []
    with open(alerts_path, "r") as fid:
        for alert in csv.reader(fid):
            alert_obj = Alert._make(alert)
            if is_whitelisted(alert_obj, whitelist):
                result.append(alert_obj)
    return result


def is_unsafe_tag(tag):
    return tag == Tag.MEMORY_MANAGEMENT_UNSAFE \
            or tag  == Tag.RACE_COND_UNSAFE \
            or tag == Tag.COND_WAIT_UNSAFE \
            or tag == Tag.COND_SIGNAL_UNSAFE \
            or tag == Tag.STRCPY_UNSAFE


def load_tags(defects_path, validation_set=None, sound_only=False):
    with open(defects_path, "r") as fid:
        defects = json.load(fid)

    result = dict()
    instance_tags = defects["tags"]

    for instance in instance_tags:
        numeric_tags = instance_tags[instance]

        if validation_set is not None:
            if instance not in validation_set:
                continue

        if sound_only:
            filtered_tags = []
            for tag_num in numeric_tags:
                filtered_tags.append(tag_num)
                if is_unsafe_tag(Tag(tag_num)):
                    break
            numeric_tags = filtered_tags

        result[instance] = numeric_tags
    return result


def load_checker_whitelist(whitelist_path):
    with open(whitelist_path, "r") as fid:
        data = yaml.load(fid)
    return data


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("whitelist")
    parser.add_argument("alert_files", nargs="+")
    parser.add_argument("--validation_set")
    parser.add_argument("--sound_only", action="store_true")
    parser.add_argument(
        '-v',
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.WARNING)

    args = parser.parse_args()
    logging.basicConfig(level=args.loglevel)

    validation_set = None
    if args.validation_set is not None:
        validation_set = set()
        with open(args.validation_set) as f:
            for line in f:
                validation_set.add(line.strip())

    instance_tags = load_tags(
        args.manifest,
        validation_set=validation_set,
        sound_only=args.sound_only)
    whitelist = load_checker_whitelist(args.whitelist)

    alerts = []
    for alert_file in args.alert_files:
        alerts += load_alerts(alert_file, whitelist)

    all_tools = set([a.tool for a in alerts])
    alert_index = defaultdict(set)
    for alert in alerts:
        alert_index[(PurePath(alert.file).name,
                     int(alert.line))].add(alert.tool)

    scores = {
        # For each tool
        tool: {
            # For each tag
            tag: {
                # True indicates a positive response
                True: 0,
                # False indicates a negative reponse
                False: 0
            }
            for tag in [e.value for e in Tag]
        }
        for tool in all_tools
    }

    for instance, tags in instance_tags.items():
        for tag, line in zip(tags, range(1, len(tags) + 1)):
            # Hits are positive response to the tag
            hits = alert_index[(instance, line)]

            for hit in hits:
                # Indicate a positive response
                scores[hit][tag][True] += 1
                logging.debug("RESPONSE,%s,%s,%s,%d",
                              Tag(tag).name, hit, instance, line)
            for other in (all_tools - hits):
                # Indicate a negative response
                scores[other][tag][False] += 1
                logging.debug("NO_RESPONSE,%s,%s,%s,%d",
                              Tag(tag).name, other, instance, line)

    tag_sets = [
        ("MEMORY_MANAGEMENT", Tag.MEMORY_MANAGEMENT_UNSAFE, Tag.MEMORY_MANAGEMENT_SAFE),
        ("RACE_CONDITION", Tag.RACE_COND_UNSAFE, Tag.RACE_COND_SAFE),
        ("CONDITION_WAIT", Tag.COND_WAIT_UNSAFE, Tag.COND_WAIT_SAFE),
        ("CONDITION_SIGNAL", Tag.COND_SIGNAL_UNSAFE, Tag.COND_SIGNAL_SAFE),
        ("STRCPY", Tag.STRCPY_UNSAFE, Tag.STRCPY_SAFE),
    ]

    writer = csv.writer(sys.stdout)
    writer.writerow(["tool", "kind", "tp", "tn", "fp", "fn"])

    for tool in sorted(scores.keys()):
        tool_scores = scores[tool]
        conf_matrices = []
        for (set_name, unsafe, safe) in tag_sets:
            conf_matrix = [
                # Positive response + UNSAFE == True Positive
                tool_scores[unsafe.value][True],
                # Negative response + SAFE == True Negative
                tool_scores[safe.value][False],
                # Positive response + SAFE == False Positive
                tool_scores[safe.value][True],
                # Negative response + UNSAFE == False Negative
                tool_scores[unsafe.value][False]
            ]
            writer.writerow([tool, set_name] + conf_matrix)
            conf_matrices.append(conf_matrix)
        combined_matrix = [sum(a) for a in zip(*conf_matrices)]
        writer.writerow([tool, "all"] + combined_matrix)
