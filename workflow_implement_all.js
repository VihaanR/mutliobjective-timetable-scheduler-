export const meta = {
  name: 'implement-timetable-features',
  description: 'Implement mid-day break, LLM constraints, and drag-and-drop',
  phases: [
    { title: 'Implementation', detail: 'Implement all requested features' },
  ],
}

const tasks = [
  { 
    name: 'mid-day-break', 
    prompt: 'Implement the hard constraint ensuring breaks are in the middle of the total workday in model.py.' 
  },
  { 
    name: 'llm-constraint-system', 
    prompt: 'Complete the implementation of the optional LLM constraint query system with Gemini API gating.' 
  },
  { 
    name: 'dashboard-drag-drop', 
    prompt: 'Implement drag-and-drop on the generated dashboard to switch classes.' 
  }
];

const results = await pipeline(
  tasks,
  t => agent(t.prompt, {label: t.name, phase: 'Implementation'})
);

return { results };
