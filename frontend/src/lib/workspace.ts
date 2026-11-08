import type { Employee, EmployeeCapabilityOption, ModelOption } from '@/types';

export function selectedEmployeeCapabilities(options: EmployeeCapabilityOption[], selections: string[]): string[] {
  const selected = new Set(selections);
  return options.filter(option => selected.has(option.name) || option.legacy_tool_names?.some(name => selected.has(name))).map(option => option.name);
}

/** 添加同事时优先推荐团队尚未使用的模型与供应商，用户仍可自由改选。 */
export function suggestEmployeeModel(models: ModelOption[], employees: Employee[], defaultModel: string, defaultProfile = ''): ModelOption | undefined {
  const team = employees.map(employee => ({ model: employee.model || defaultModel, profile: employee.model_profile_id || defaultProfile }));
  const score = (option: ModelOption) => team.filter(employee => employee.model === option.id).length * 10
    + team.filter(employee => employee.profile === (option.profile_id || '')).length;
  return models.slice().sort((a, b) => score(a) - score(b) || Number(b.default) - Number(a.default))[0];
}
