import configparser
import os
import re
from typing import Any

import boto3
import requests
import streamlit as st
from langchain_aws import ChatBedrock
from langchain_aws.graphs import NeptuneRdfGraph
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI


CONFIG_FILE_PATH = os.path.join(os.path.expanduser("~"), "streamlit", "settings.cfg")
DEFAULT_GRAPH_BACKEND = "fuseki"
DEFAULT_FUSEKI_ENDPOINT = "http://localhost:3031/kg/sparql"
DEFAULT_NEPTUNE_PORT = 8182
DEFAULT_REGION = "us-east-1"
DEFAULT_LLM_PROVIDER = "ollama"
DEFAULT_MODELS = {
    "ollama": "llama3.2:3b",
    "openai": "gpt-4.1-mini",
    "bedrock": "anthropic.claude-sonnet-4-5-20250929-v1:0",
}
READ_ONLY_KEYWORDS = re.compile(
    r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|MOVE|COPY|ADD)\b",
    re.IGNORECASE,
)

logger = st.logger.get_logger(__name__)


class FusekiSparqlGraph:
    def __init__(
        self,
        endpoint_url: str,
        username: str = "",
        password: str = "",
        timeout: int = 60,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.username = username
        self.password = password
        self.timeout = timeout
        self._schema: str | None = None

    @property
    def get_schema(self) -> str:
        if self._schema is None:
            self._schema = self._load_schema()
        return self._schema

    def query(self, sparql: str) -> dict[str, Any]:
        auth = (self.username, self.password) if self.username else None
        response = requests.post(
            self.endpoint_url,
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
            auth=auth,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return self._simplify_result(data)

    def _load_schema(self) -> str:
        classes = self._select_values(
            """
            SELECT DISTINCT ?class
            WHERE { ?s a ?class . }
            ORDER BY ?class
            LIMIT 100
            """,
            "class",
        )
        predicates = self._select_values(
            """
            SELECT DISTINCT ?predicate
            WHERE { ?s ?predicate ?o . }
            ORDER BY ?predicate
            LIMIT 150
            """,
            "predicate",
        )

        class_lines = "\n".join(f"- <{value}> ({local_name(value)})" for value in classes)
        predicate_lines = "\n".join(
            f"- <{value}> ({local_name(value)})" for value in predicates
        )
        return (
            "RDF graph schema discovered from the SPARQL endpoint.\n"
            "Classes:\n"
            f"{class_lines or '- No classes discovered'}\n\n"
            "Predicates:\n"
            f"{predicate_lines or '- No predicates discovered'}"
        )

    def _select_values(self, sparql: str, variable: str) -> list[str]:
        try:
            result = self.query(sparql)
        except requests.RequestException as exc:
            logger.warning("Schema discovery query failed: %s", exc)
            return []

        values = []
        for row in result.get("rows", []):
            value = row.get(variable)
            if value:
                values.append(str(value))
        return values

    @staticmethod
    def _simplify_result(data: dict[str, Any]) -> dict[str, Any]:
        if "boolean" in data:
            return {"boolean": data["boolean"], "rows": []}

        variables = data.get("head", {}).get("vars", [])
        rows = []
        for binding in data.get("results", {}).get("bindings", []):
            row = {}
            for variable in variables:
                if variable in binding:
                    row[variable] = binding[variable].get("value")
            rows.append(row)
        return {"vars": variables, "rows": rows}


class ReadOnlyKnowledgeGraphRag:
    def __init__(self, graph: Any, llm: BaseChatModel, graph_backend: str) -> None:
        self.graph = graph
        self.llm = llm
        self.graph_backend = graph_backend
        self.sparql_chain = self._sparql_prompt() | self.llm | StrOutputParser()
        self.answer_chain = self._answer_prompt() | self.llm | StrOutputParser()
        logger.info("RAG chain initialized for %s", graph_backend)

    def invoke(self, question: str) -> dict[str, Any]:
        schema = self.graph.get_schema
        sparql = self._clean_sparql(
            self.sparql_chain.invoke(
                {
                    "graph_backend": self.graph_backend,
                    "schema": schema,
                    "question": question,
                }
            )
        )
        self._validate_read_only_query(sparql)
        context = self.graph.query(sparql)
        answer = self.answer_chain.invoke(
            {"question": question, "sparql": sparql, "context": context}
        )
        return {"result": answer, "sparql_query": sparql, "context": context}

    @staticmethod
    def _sparql_prompt() -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You generate read-only SPARQL SELECT queries for an RDF graph. "
                    "The graph backend is {graph_backend}. Use only classes and "
                    "properties from the schema. Include every required PREFIX. "
                    "Return only the SPARQL query, with no markdown, comments, or "
                    "explanation. Never generate SPARQL UPDATE, INSERT, DELETE, LOAD, "
                    "CLEAR, CREATE, DROP, MOVE, COPY, or ADD statements.",
                ),
                ("human", "Schema:\n{schema}\n\nQuestion:\n{question}"),
            ]
        )

    @staticmethod
    def _answer_prompt() -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Answer using only the SPARQL result context. If the context does "
                    "not contain enough information, say that the graph did not return "
                    "enough information. Be concise and do not add outside knowledge.",
                ),
                (
                    "human",
                    "Question:\n{question}\n\nSPARQL:\n{sparql}\n\nContext:\n{context}",
                ),
            ]
        )

    @staticmethod
    def _clean_sparql(text: str) -> str:
        text = text.strip()
        fenced = re.search(
            r"```(?:sparql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL
        )
        if fenced:
            text = fenced.group(1).strip()
        return text

    @staticmethod
    def _validate_read_only_query(sparql: str) -> None:
        normalized = sparql.strip().upper()
        if READ_ONLY_KEYWORDS.search(normalized):
            raise ValueError("Only read-only SPARQL SELECT queries are allowed.")
        if "SELECT" not in normalized:
            raise ValueError("The generated SPARQL must be a SELECT query.")


def local_name(uri: str) -> str:
    return re.split(r"[/#]", uri.rstrip("/#"))[-1]


def load_settings() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE_PATH)
    return config


def get_setting(config: configparser.ConfigParser, key: str, fallback: str) -> str:
    return config.get("default", key, fallback=fallback)


def get_settings() -> dict[str, Any]:
    config = load_settings()
    llm_provider = get_setting(config, "llm_provider", DEFAULT_LLM_PROVIDER)
    return {
        "graph_backend": get_setting(config, "graph_backend", DEFAULT_GRAPH_BACKEND),
        "fuseki_endpoint": get_setting(
            config, "fuseki_endpoint", DEFAULT_FUSEKI_ENDPOINT
        ),
        "fuseki_username": get_setting(config, "fuseki_username", ""),
        "fuseki_password": get_setting(config, "fuseki_password", ""),
        "neptune_host": get_setting(config, "neptune_host", ""),
        "neptune_port": config.getint(
            "default", "neptune_port", fallback=DEFAULT_NEPTUNE_PORT
        ),
        "region": get_setting(config, "region", DEFAULT_REGION),
        "llm_provider": llm_provider,
        "model_id": get_setting(
            config, "model_id", DEFAULT_MODELS.get(llm_provider, DEFAULT_MODELS["ollama"])
        ),
    }


def save_settings(settings: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE_PATH), exist_ok=True)
    config = configparser.ConfigParser()
    config["default"] = {key: str(value) for key, value in settings.items()}
    with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as configfile:
        config.write(configfile)
    st.session_state.pop("rag_chain", None)
    st.session_state.pop("rag_chain_key", None)


def create_graph(settings: dict[str, Any]) -> Any:
    if settings["graph_backend"] == "fuseki":
        if not settings["fuseki_endpoint"]:
            raise ValueError("Set the Fuseki SPARQL endpoint on the Settings page.")
        return FusekiSparqlGraph(
            endpoint_url=settings["fuseki_endpoint"],
            username=settings["fuseki_username"],
            password=settings["fuseki_password"],
        )

    if not settings["neptune_host"]:
        raise ValueError("Set the Neptune host on the Settings page.")
    return NeptuneRdfGraph(
        host=settings["neptune_host"],
        port=int(settings["neptune_port"]),
        use_iam_auth=True,
        region_name=settings["region"],
        use_https=True,
    )


def create_llm(settings: dict[str, Any]) -> BaseChatModel:
    provider = settings["llm_provider"]
    model_id = settings["model_id"]

    if provider == "ollama":
        return ChatOllama(model=model_id, temperature=0)
    if provider == "openai":
        return ChatOpenAI(model=model_id, temperature=0)
    if provider == "bedrock":
        bedrock_client = boto3.client(
            "bedrock-runtime", region_name=settings["region"]
        )
        return ChatBedrock(
            model_id=model_id,
            client=bedrock_client,
            temperature=0,
            max_tokens=2048,
        )
    raise ValueError(f"Unsupported LLM provider: {provider}")


def get_chain(settings: dict[str, Any]) -> ReadOnlyKnowledgeGraphRag:
    chain_key = (
        settings["graph_backend"],
        settings["fuseki_endpoint"],
        settings["fuseki_username"],
        settings["neptune_host"],
        settings["neptune_port"],
        settings["region"],
        settings["llm_provider"],
        settings["model_id"],
    )
    if (
        st.session_state.get("rag_chain_key") != chain_key
        or "rag_chain" not in st.session_state
    ):
        graph = create_graph(settings)
        llm = create_llm(settings)
        st.session_state["rag_chain"] = ReadOnlyKnowledgeGraphRag(
            graph=graph,
            llm=llm,
            graph_backend=settings["graph_backend"],
        )
        st.session_state["rag_chain_key"] = chain_key
    return st.session_state["rag_chain"]


def app() -> None:
    st.set_page_config(page_title="Knowledge Graph RAG")
    pages = {
        "Settings": settings_page,
        "RAG": rag_page,
        "SPARQL": sparql_page,
    }

    st.sidebar.title("Navigation")
    selection = st.sidebar.radio("Go to", list(pages.keys()))
    pages[selection]()


def settings_page() -> None:
    st.title("Settings")
    settings = get_settings()

    graph_backend = st.selectbox(
        "Graph Backend",
        ["fuseki", "neptune"],
        index=["fuseki", "neptune"].index(settings["graph_backend"]),
    )

    fuseki_endpoint = settings["fuseki_endpoint"]
    fuseki_username = settings["fuseki_username"]
    fuseki_password = settings["fuseki_password"]
    neptune_host = settings["neptune_host"]
    neptune_port = settings["neptune_port"]

    if graph_backend == "fuseki":
        fuseki_endpoint = st.text_input("Fuseki SPARQL Endpoint", fuseki_endpoint)
        fuseki_username = st.text_input("Fuseki Username", fuseki_username)
        fuseki_password = st.text_input(
            "Fuseki Password", fuseki_password, type="password"
        )
    else:
        neptune_host = st.text_input("Neptune Host", neptune_host)
        neptune_port = st.number_input("Neptune Port", value=neptune_port)

    region = st.text_input("AWS Region", settings["region"])
    llm_provider = st.selectbox(
        "LLM Provider",
        ["ollama", "openai", "bedrock"],
        index=["ollama", "openai", "bedrock"].index(settings["llm_provider"]),
    )
    default_model = (
        settings["model_id"]
        if settings["llm_provider"] == llm_provider
        else DEFAULT_MODELS[llm_provider]
    )
    model_id = st.text_input("Model ID", value=default_model)

    if st.button("Save Settings"):
        save_settings(
            {
                "graph_backend": graph_backend,
                "fuseki_endpoint": fuseki_endpoint,
                "fuseki_username": fuseki_username,
                "fuseki_password": fuseki_password,
                "neptune_host": neptune_host,
                "neptune_port": int(neptune_port),
                "region": region,
                "llm_provider": llm_provider,
                "model_id": model_id,
            }
        )
        st.success("Settings saved successfully.")


def rag_page() -> None:
    st.title("Retrieval Augmented Generation with Knowledge Graphs using SPARQL")
    query = st.text_area("Enter your query")

    if st.button("Submit"):
        if not query.strip():
            st.warning("Enter a question first.")
            return

        try:
            chain = get_chain(get_settings())
            result = chain.invoke(query)
        except Exception as exc:
            logger.exception("RAG request failed")
            st.error(f"RAG request failed: {exc}")
            return

        st.write("Result:")
        st.write(result["result"])
        st.write("Generated SPARQL:")
        st.code(result["sparql_query"], language="sparql")
        st.write("Full Context:")
        st.json(result["context"], expanded=False)


def sparql_page() -> None:
    st.title("SPARQL")
    settings = get_settings()
    sparql = st.text_area(
        "Query",
        value="""PREFIX ex: <http://example.org/kg/>

SELECT ?movie ?title
WHERE {
  ?movie a ex:Movie ;
         ex:title ?title .
}
LIMIT 10""",
        height=220,
    )

    if st.button("Run SPARQL"):
        try:
            graph = create_graph(settings)
            ReadOnlyKnowledgeGraphRag._validate_read_only_query(sparql)
            st.json(graph.query(sparql), expanded=True)
        except Exception as exc:
            logger.exception("SPARQL query failed")
            st.error(f"SPARQL query failed: {exc}")


if __name__ == "__main__":
    app()
