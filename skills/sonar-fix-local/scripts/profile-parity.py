"""核对同版本 Sonar 导出的规则语义；不执行来自规则说明的内容。"""
import json
import pathlib
import sys
import xml.etree.ElementTree as ET


def rules(file):
    result = {}
    for rule in ET.parse(file).getroot().findall('./rules/rule'):
        key = rule.findtext('repositoryKey') + ':' + rule.findtext('key')
        if key in result:
            raise ValueError('重复规则编号')
        result[key] = {
            'priority': rule.findtext('priority'),
            'type': rule.findtext('type'),
            'params': sorted((p.findtext('key'), p.findtext('value') or '') for p in rule.findall('./parameters/parameter')),
            'impacts': sorted((p.findtext('softwareQuality'), p.findtext('severity')) for p in rule.findall('./impacts/impact')),
        }
    return result


def verify(run):
    base = pathlib.Path(run) / 'config'
    load = lambda name: json.loads((base / name).read_text())
    profiles = []
    for p in load('profiles.json'):
        a, b = rules(base / (p['language'] + '.xml')), rules(base / ('local-' + p['language'] + '.xml'))
        if a != b or len(a) != p['activeRules']:
            raise ValueError('规则编号、数量、参数或严重级别不一致：' + p['language'])
        profiles.append({'language': p['language'], 'rules': len(a), 'match': True})
    online = {p['key']: p.get('version') for p in load('plugins.json')['plugins']}
    local = {p['key']: p.get('version') for p in load('local-plugins.json')['plugins']}
    analyzers = ['javascript', 'web', 'iac', 'text']
    if any(p['language'] == 'yaml' for p in profiles):
        analyzers.append('yaml')
    for key in analyzers:
        if key not in online or online[key] != local.get(key):
            raise ValueError('分析器版本不一致：' + key)
    conditions = lambda data: sorted((c['metric'], c['op'], c['error']) for c in data['conditions'])
    if conditions(load('gate.json')) != conditions(load('local-gate.json')):
        raise ValueError('门禁条件不一致')
    result = {'profiles': profiles, 'analyzers': {k: online[k] for k in analyzers}, 'gateMatch': True}
    (pathlib.Path(run) / 'config-parity.json').write_text(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    try:
        verify(sys.argv[1])
        print('规则、分析器与门禁配置比对通过；新代码门禁仍未评估')
    except Exception as error:
        sys.exit(str(error))
