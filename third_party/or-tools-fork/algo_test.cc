// Standalone verification of the algorithms added to the OR-Tools fork.
//
// Full OR-Tools cannot be built on this machine (no MSVC/CMake), so this
// harness lifts the three pieces of new *logic* out of the patch verbatim --
// only the absl types are swapped for std equivalents -- and exercises them
// against the real 4558 variable names emitted by engine/solvers/cpsat.py.
//
// What this proves: the algorithms compile and behave correctly.
// What this does NOT prove: that the patch integrates with OR-Tools' types,
// includes and build graph. Only a real build settles that.

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <numeric>
#include <random>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// ---------------------------------------------------------------------------
// Verbatim from cp_model_utils.cc (absl::string_view -> std::string_view)
// ---------------------------------------------------------------------------
std::string_view ExtractDomainTag(std::string_view var_name,
                                  std::string_view tag) {
  const std::string_view::size_type start = var_name.find(tag);
  if (start == std::string_view::npos) return std::string_view();
  const std::string_view::size_type value_start = start + tag.size();
  const std::string_view::size_type end = var_name.find('@', value_start);
  if (end == std::string_view::npos) return var_name.substr(value_start);
  return var_name.substr(value_start, end - value_start);
}

std::vector<std::vector<int>> GroupVariablesByDomainTag(
    const std::vector<std::string>& var_names, std::string_view tag) {
  std::vector<std::vector<int>> groups;
  std::unordered_map<std::string_view, int> group_index;
  for (int var = 0; var < static_cast<int>(var_names.size()); ++var) {
    const std::string_view key = ExtractDomainTag(var_names[var], tag);
    if (key.empty()) continue;
    const auto [it, inserted] =
        group_index.insert({key, static_cast<int>(groups.size())});
    if (inserted) groups.push_back({});
    groups[it->second].push_back(var);
  }
  return groups;
}

// ---------------------------------------------------------------------------
// Verbatim selection logic from DivisionDayNeighborhoodGenerator::Generate
// ---------------------------------------------------------------------------
std::vector<int> SelectRelaxedVariables(
    const std::vector<std::vector<int>>& groups,
    const std::vector<int>& always_relaxed, double difficulty,
    std::mt19937& rng, bool* was_full) {
  *was_full = false;
  const int num_groups = static_cast<int>(groups.size());
  const int num_to_relax =
      std::max(1, static_cast<int>(std::round(difficulty * num_groups)));
  if (num_to_relax >= num_groups) {
    *was_full = true;
    return {};
  }
  std::vector<int> order(num_groups);
  std::iota(order.begin(), order.end(), 0);
  for (int i = 0; i < num_to_relax; ++i) {
    std::uniform_int_distribution<int> d(i, num_groups - 1);  // absl [i, n)
    std::swap(order[i], order[d(rng)]);
  }
  std::vector<int> relaxed = always_relaxed;
  for (int i = 0; i < num_to_relax; ++i) {
    relaxed.insert(relaxed.end(), groups[order[i]].begin(),
                   groups[order[i]].end());
  }
  return relaxed;
}

// ---------------------------------------------------------------------------
// Verbatim scoring from the CHOOSE_MIN_UNFIXED_IN_GROUP case
// ---------------------------------------------------------------------------
int64_t ScoreVariable(int var, const std::vector<int>& var_to_group,
                      const std::vector<int>& group_unfixed) {
  const int group =
      var < static_cast<int>(var_to_group.size()) ? var_to_group[var] : -1;
  return group < 0 ? std::numeric_limits<int64_t>::max() - 1
                   : group_unfixed[group];
}

static int failures = 0;
#define CHECK_THAT(cond, msg)                                    \
  do {                                                           \
    if (!(cond)) {                                               \
      std::cout << "  FAIL: " << (msg) << "\n";                  \
      ++failures;                                                \
    }                                                            \
  } while (0)

int main(int argc, char** argv) {
  // --- 1. ExtractDomainTag edge cases ------------------------------------
  std::cout << "[1] ExtractDomainTag edge cases\n";
  CHECK_THAT(ExtractDomainTag("x_a_b@R=req1@G=D1#3", "@R=") == "req1",
             "tag followed by another tag");
  CHECK_THAT(ExtractDomainTag("x_a_b@R=req1@G=D1#3", "@G=") == "D1#3",
             "trailing tag runs to end of string");
  CHECK_THAT(ExtractDomainTag("occ_D1_0_0", "@G=").empty(),
             "absent tag yields empty");
  CHECK_THAT(ExtractDomainTag("x@R=@G=D1#0", "@R=").empty(),
             "empty tag value yields empty (and is skipped by grouping)");
  CHECK_THAT(ExtractDomainTag("x_plain", "@R=").empty(), "no tags at all");

  // --- 2. Grouping against the real model --------------------------------
  std::cout << "[2] Grouping over real cpsat.py variable names\n";
  if (argc < 2) {
    std::cout << "  FAIL: expected path to varnames.txt\n";
    return 1;
  }
  std::ifstream in(argv[1]);
  std::vector<std::string> names;
  for (std::string line; std::getline(in, line);) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    names.push_back(line);
  }
  std::cout << "  loaded " << names.size() << " variable names\n";
  CHECK_THAT(names.size() == 4558, "expected 4558 variables");

  const auto g_groups = GroupVariablesByDomainTag(names, "@G=");
  const auto r_groups = GroupVariablesByDomainTag(names, "@R=");
  std::cout << "  @G= groups: " << g_groups.size()
            << " | @R= groups: " << r_groups.size() << "\n";
  // These are the counts Python independently computed on the same model.
  CHECK_THAT(g_groups.size() == 10, "expected 10 division-day groups");
  CHECK_THAT(r_groups.size() == 76, "expected 76 requirement groups");

  size_t tagged = 0;
  for (const auto& g : g_groups) tagged += g.size();
  std::vector<int> always_relaxed;
  for (int i = 0; i < static_cast<int>(names.size()); ++i) {
    if (ExtractDomainTag(names[i], "@G=").empty()) always_relaxed.push_back(i);
  }
  std::cout << "  tagged: " << tagged
            << " | untagged (always_relaxed): " << always_relaxed.size()
            << "\n";
  CHECK_THAT(tagged == 4250, "expected 4250 tagged placement variables");
  CHECK_THAT(always_relaxed.size() == 308, "expected 308 untagged variables");
  CHECK_THAT(tagged + always_relaxed.size() == names.size(),
             "tagged + untagged must partition all variables");

  // Every variable appears in exactly one group (no double counting).
  std::unordered_set<int> seen;
  for (const auto& g : g_groups) {
    for (int v : g) {
      CHECK_THAT(seen.insert(v).second, "variable appears in two @G= groups");
    }
  }

  // Determinism: first-seen order must be stable, not hash order.
  const auto g_again = GroupVariablesByDomainTag(names, "@G=");
  CHECK_THAT(g_groups == g_again, "grouping is not deterministic");

  // --- 3. Neighborhood selection -----------------------------------------
  std::cout << "[3] DivisionDay neighborhood selection\n";
  std::mt19937 rng(12345);
  bool full = false;

  auto relaxed = SelectRelaxedVariables(g_groups, always_relaxed, 0.0, rng, &full);
  CHECK_THAT(!full, "difficulty 0.0 must not be the full neighborhood");
  // floor of one group: 308 auxiliary + exactly one division-day's variables.
  CHECK_THAT(relaxed.size() > always_relaxed.size(),
             "difficulty 0.0 must still relax one whole division-day");
  std::cout << "  difficulty 0.0 -> relaxed " << relaxed.size()
            << " vars (>" << always_relaxed.size() << " auxiliary)\n";

  relaxed = SelectRelaxedVariables(g_groups, always_relaxed, 1.0, rng, &full);
  CHECK_THAT(full, "difficulty 1.0 must fall back to the full neighborhood");

  // Mid difficulty: distinct groups, auxiliary always included.
  relaxed = SelectRelaxedVariables(g_groups, always_relaxed, 0.5, rng, &full);
  CHECK_THAT(!full, "difficulty 0.5 must not be full");
  std::unordered_set<int> uniq(relaxed.begin(), relaxed.end());
  CHECK_THAT(uniq.size() == relaxed.size(),
             "relaxed set contains duplicates (groups overlapped)");
  for (int v : always_relaxed) {
    CHECK_THAT(uniq.count(v) == 1, "auxiliary variable was not relaxed");
  }
  std::cout << "  difficulty 0.5 -> relaxed " << relaxed.size()
            << " vars, all distinct, auxiliary included\n";

  // Fisher-Yates must be able to reach every group over many draws.
  std::unordered_set<int> ever;
  for (int t = 0; t < 400; ++t) {
    bool f = false;
    auto r = SelectRelaxedVariables(g_groups, {}, 0.0, rng, &f);
    ever.insert(r.begin(), r.end());
  }
  size_t reachable = 0;
  for (const auto& g : g_groups) {
    if (ever.count(g[0])) ++reachable;
  }
  CHECK_THAT(reachable == g_groups.size(),
             "some division-day is never selectable");
  std::cout << "  all " << reachable << "/" << g_groups.size()
            << " division-days reachable by sampling\n";

  // --- 4. MRV scoring -----------------------------------------------------
  std::cout << "[4] CHOOSE_MIN_UNFIXED_IN_GROUP scoring\n";
  std::vector<int> var_to_group(names.size(), -1);
  for (int g = 0; g < static_cast<int>(r_groups.size()); ++g) {
    for (int v : r_groups[g]) var_to_group[v] = g;
  }
  std::vector<int> group_unfixed(r_groups.size(), 0);
  for (size_t g = 0; g < r_groups.size(); ++g) {
    group_unfixed[g] = static_cast<int>(r_groups[g].size());
  }
  // Make one requirement the most-constrained one and confirm it wins.
  const int tight = 7;
  group_unfixed[tight] = 1;
  int64_t best = std::numeric_limits<int64_t>::max();
  int best_var = -1;
  for (int v = 0; v < static_cast<int>(names.size()); ++v) {
    if (var_to_group[v] < 0) continue;  // skip untagged for this check
    const int64_t s = ScoreVariable(v, var_to_group, group_unfixed);
    if (s < best) {
      best = s;
      best_var = v;
    }
  }
  CHECK_THAT(best_var >= 0 && var_to_group[best_var] == tight,
             "MRV did not pick the most-constrained requirement");
  std::cout << "  picked group " << var_to_group[best_var] << " (score " << best
            << ") == most-constrained group " << tight << "\n";

  // An untagged variable must sort last but remain selectable.
  const int64_t untagged_score = ScoreVariable(
      always_relaxed.empty() ? 0 : always_relaxed[0], var_to_group,
      group_unfixed);
  CHECK_THAT(untagged_score == std::numeric_limits<int64_t>::max() - 1,
             "untagged variable score must be max-1");
  CHECK_THAT(untagged_score < std::numeric_limits<int64_t>::max(),
             "untagged variable must still beat the initial candidate_value");
  std::cout << "  untagged sorts last but stays selectable\n";

  std::cout << (failures == 0 ? "\nALL CHECKS PASSED\n"
                              : "\nFAILURES PRESENT\n");
  return failures == 0 ? 0 : 1;
}
