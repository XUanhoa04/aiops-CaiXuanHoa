import json
import warnings
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

warnings.filterwarnings('ignore')

with open('dataset/incidents_history.json', 'r', encoding='utf-8') as f:
    incidents = json.load(f)['incidents']

all_services = sorted(list(set(svc for inc in incidents for svc in inc['services_involved'])))

def extract_features(inc):
    feats = [1 if svc in inc['services_involved'] else 0 for svc in all_services]
    sev_map = {'low': 1, 'medium': 2, 'high': 3, 'critical': 4}
    feats.append(sev_map.get(inc['severity'], 0))
    # use mttd_min as a proxy for time_burst_pattern
    feats.append(inc.get('mttd_min', 0))
    return feats

X = [extract_features(inc) for inc in incidents]
y = [inc['root_cause_class'] for inc in incidents]

# Train-test split
X_train, X_test, y_train, y_test, inc_train, inc_test = train_test_split(
    X, y, incidents, test_size=0.3, random_state=42
)

# 1. Decision Tree
clf = DecisionTreeClassifier(random_state=42)
clf.fit(X_train, y_train)
dt_preds = clf.predict(X_test)
dt_acc = accuracy_score(y_test, dt_preds)

# 2. kNN Heuristic
def retrieve_similar(target_inc, history_incs):
    cluster_services = set(target_inc["services_involved"])
    cluster_sev = target_inc["severity"]
    
    scored_incidents = []
    for inc in history_incs:
        score = 0.0
        if inc["root_cause_service"] in cluster_services: score += 0.4
        overlap = len(cluster_services.intersection(set(inc["services_involved"])))
        score += min(0.4, 0.2 * overlap)
        if inc["severity"] == cluster_sev: score += 0.2
        if score >= 0.2:
            scored_incidents.append((score, inc))
            
    scored_incidents.sort(key=lambda x: x[0], reverse=True)
    return scored_incidents[0][1] if scored_incidents else None

knn_preds = []
for target_inc in inc_test:
    best_match = retrieve_similar(target_inc, inc_train)
    if best_match:
        knn_preds.append(best_match['root_cause_class'])
    else:
        knn_preds.append("other")

knn_acc = accuracy_score(y_test, knn_preds)

print(f"Decision Tree Accuracy (Test Set): {dt_acc:.2%}")
print(f"kNN Heuristic Accuracy (Test Set): {knn_acc:.2%}")
