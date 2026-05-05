from packages.shared.sales_intents import analyze_sales_message


def test_sales_intent_analyzer_routes_promotions_as_catalog_request():
    analysis = analyze_sales_message("q promociones tienes?")

    assert analysis.intent == "plan"
    assert analysis.has_explicit_catalog_terms is True
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_routes_promos_shorthand_as_catalog_request():
    analysis = analyze_sales_message("q promos tienes?")

    assert analysis.intent == "plan"
    assert analysis.has_explicit_catalog_terms is True
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_does_not_confuse_promotions_with_agency_followup():
    analysis = analyze_sales_message("y que promociones tiene")

    assert analysis.intent == "plan"
    assert analysis.is_agency_followup is False


def test_sales_intent_analyzer_tolerates_typos_in_catalog_question():
    analysis = analyze_sales_message("q proms tinees?")

    assert analysis.intent == "plan"
    assert analysis.has_explicit_catalog_terms is True
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_keeps_generic_internet_issue_out_of_sales_classifier():
    analysis = analyze_sales_message("internet lento")

    assert analysis.intent == "plan"
    assert analysis.has_explicit_catalog_terms is False
    assert analysis.routes_to_sales_classifier is False


def test_sales_intent_analyzer_treats_quiero_internet_as_explicit_commercial_info():
    analysis = analyze_sales_message("Quiero internet")

    assert analysis.intent == "info"
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_treats_publication_interest_as_commercial_info():
    analysis = analyze_sales_message("vi una publicacion de internet")

    assert analysis.intent == "info"
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_detects_acceptance_of_recommended_plan():
    analysis = analyze_sales_message("ese me sirve")

    assert analysis.accepts_recommended_plan is True
    assert analysis.wants_capture is False


def test_sales_intent_analyzer_detects_bare_deictic_acceptance():
    analysis = analyze_sales_message("ese")

    assert analysis.accepts_recommended_plan is True


def test_sales_intent_analyzer_detects_ese_sirve_acceptance():
    analysis = analyze_sales_message("ese sirve")

    assert analysis.accepts_recommended_plan is True


def test_sales_intent_analyzer_does_not_confuse_rejection_with_acceptance():
    analysis = analyze_sales_message("ese no me sirve")

    assert analysis.accepts_recommended_plan is False


def test_sales_intent_analyzer_detects_personalized_recommendation_choice():
    analysis = analyze_sales_message("quiero una recomendación personalizada")

    assert analysis.wants_personalized_recommendation is True
    assert analysis.wants_full_catalog is False


def test_sales_intent_analyzer_detects_acceptance_with_este_wording():
    analysis = analyze_sales_message("avancemos con este")

    assert analysis.accepts_recommended_plan is True


def test_sales_intent_analyzer_detects_show_me_the_rest_after_recommendation():
    analysis = analyze_sales_message("muéstrame los demás")

    assert analysis.wants_full_catalog is True


def test_sales_intent_analyzer_treats_solo_quiero_los_planes_as_catalog_request():
    analysis = analyze_sales_message("no joda, solo quiero los planes")

    assert analysis.wants_full_catalog is True


def test_sales_intent_analyzer_treats_dame_los_planes_as_catalog_request():
    analysis = analyze_sales_message("no jodas y dame los planes")

    assert analysis.intent == "plan"
    assert analysis.has_explicit_catalog_terms is True
    assert analysis.wants_full_catalog is True
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_treats_que_internets_ofrecen_as_discovery():
    analysis = analyze_sales_message("hola q internets ofrecen?")

    assert analysis.is_discovery_query is True
    assert analysis.routes_to_sales_classifier is True


def test_sales_intent_analyzer_treats_typos_in_que_internet_ofreces_as_discovery():
    analysis = analyze_sales_message("queee intrsnet ofreces?")

    assert analysis.intent == "discovery"
    assert analysis.is_discovery_query is True
    assert analysis.routes_to_sales_classifier is True
