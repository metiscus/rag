"""
Runs a RAG application backed by a txtai Embeddings database.
"""

import os
import platform
import re

from glob import glob
from io import BytesIO
from uuid import UUID

from PIL import Image
from tqdm import tqdm

import matplotlib.pyplot as plt
import networkx as nx
import streamlit as st

from txtai import Embeddings, LLM, RAG
from txtai.pipeline import Textractor

# Build logger
logger = st.logger.get_logger(__name__)


class AutoId:
    """
    Helper methods to detect txtai auto ids
    """

    @staticmethod
    def valid(uid):
        """
        Checks if uid is a valid auto id (UUID or numeric id).

        Args:
            uid: input id

        Returns:
            True if this is an autoid, False otherwise
        """

        # Check if this is a UUID
        try:
            return UUID(str(uid))
        except ValueError:
            pass

        # Return True if this is numeric, False otherwise
        return isinstance(uid, int) or uid.isdigit()


class GraphContext:
    """
    Builds graph contexts for GraphRAG
    """

    def __init__(self, embeddings, context):
        """
        Creates a new GraphContext.

        Args:
            embeddings: embeddings instance
            context: number of records to use as context
        """

        self.embeddings = embeddings
        self.context = context

    def __call__(self, question):
        """
        Attempts to create a graph context for the input question. This method checks if:
          - Embeddings has a graph
          - Question is a graph query

        If both of the above are true, the graph is scanned to find the best matching records
        to use as a context.

        Args:
            question: input question

        Returns:
            question, [context]
        """

        query, concepts, context = self.parse(question)
        if self.embeddings.graph and (query or concepts):
            # Generate graph path query
            path = self.path(query, concepts)

            # Build graph network from path query
            graph = self.embeddings.graph.search(path, graph=True)
            if graph.count():
                # Draw and display graph
                response = self.plot(graph)
                st.write(response)
                st.session_state.messages.append(
                    {"role": "assistant", "content": response}
                )

                # Build graph context
                context = [
                    {
                        "id": graph.attribute(node, "id"),
                        "text": graph.attribute(node, "text"),
                    }
                    for node in list(graph.scan())
                ]
                if context:
                    # Default prompt
                    default = (
                        "Write a title and text summarizing the context.\n"
                        f"Include the following concepts: {concepts} if they're mentioned in the context."
                    )

                    # Set question to query if available, otherwise use default prompt
                    question = query if query else default

        return question, context

    def parse(self, question):
        """
        Attempts to parse question as a graph query. This method will return either a query
        or concepts if this is a graph query. Otherwise, both will be None.

        Args:
            question: input question

        Returns:
            query, concepts, context
        """

        # Graph query prefix
        prefix = "gq: "

        # Parse graph query
        query, concepts, context = None, None, None
        if "->" in question or question.strip().lower().startswith(prefix):
            # Split into concepts
            concepts = [x.strip() for x in question.strip().lower().split("->")]

            # Parse query out of concepts, if necessary
            if prefix in concepts[-1]:
                query, concepts = concepts[-1], concepts[:-1]

                # Look for search prefix
                query = [x.strip() for x in query.split(prefix, 1)]

                # Add concept, if necessary
                if query[0]:
                    concepts.append(query[0])

                # Extract query, if present
                if len(query) > 1:
                    query = query[1]

        return query, concepts, context

    def path(self, question, concepts):
        """
        Creates a graph path query with one of two strategies.
          - If an array of concepts is provided, the best matching row is found for each graph node
          - Otherwise, the top 3 nodes running an embeddings search for query are used

        Each node is then joined together in as a Cypher MATCH PATH query and returned.

        Args:
            question: input question
            concepts: input concepts

        Returns:
            MATCH PATH query
        """

        # Find graph nodes
        ids = []
        if concepts:
            for concept in concepts:
                uid = self.embeddings.search(concept, 1)[0]["id"]
                ids.append(f'({{id: "{uid}"}})')
        else:
            for x in self.embeddings.search(question, 3):
                ids.append(f"({{id: \"{x['id']}\"}})")

        # Create graph path query
        ids = "-[*1..4]->".join(ids)
        query = f"MATCH P={ids} RETURN P LIMIT {self.context}"
        logger.debug(query)

        return query

    def plot(self, graph):
        """
        Plot graph as an image.

        Args:
            graph: input graph

        Returns:
            Image
        """

        # Deduplicate and label graph
        graph, labels = self.deduplicate(graph, 0.9)

        options = {
            "node_size": 700,
            "node_color": "#ffbd45",
            "edge_color": "#e9ecef",
            "font_color": "#454545",
            "font_size": 10,
            "alpha": 1.0,
        }

        # Draw graph
        _, ax = plt.subplots(figsize=(9, 5))
        pos = nx.spring_layout(graph.backend, seed=0, k=0.9, iterations=50)
        nx.draw_networkx(graph.backend, pos=pos, labels=labels, **options)

        # Disable axes and draw margins
        ax.axis("off")
        plt.margins(x=0.15)

        # Save and return image
        buffer = BytesIO()
        plt.savefig(buffer, format="png", bbox_inches="tight")
        buffer.seek(0)
        return Image.open(buffer)

    def deduplicate(self, graph, threshold):
        """
        Deduplicates input graph. This method merges nodes with topics having a similarity of more
        than the input threshold. This method also builds a dictionary of labels for each node.

        Args:
            graph: input graph
            threshold: topic merge threshold

        Returns:
            graph, labels
        """

        labels, topics, deletes = {}, {}, []
        for node in graph.scan():
            uid, topic = graph.attribute(node, "id"), graph.attribute(node, "topic")
            label = topic if AutoId.valid(uid) and topic else uid

            # Find similar topics
            topicnames = list(topics.keys())
            pid, pscore = (
                self.embeddings.similarity(label, topicnames)[0]
                if topicnames
                else (0, 0.0)
            )
            primary = topics[topicnames[pid]] if pscore >= threshold else None

            if not primary:
                # Set primary node
                labels[node], topics[label] = label, node
            else:
                # Copy edges to primary node
                logger.debug(f"DUPLICATE {label} - {topicnames[pid]}")
                edges = graph.edges(node)
                if edges:
                    for target, attributes in graph.edges(node).items():
                        if primary != target:
                            graph.addedge(primary, target, **attributes)

                # Add duplicate node to delete list
                deletes.append(node)

        # Delete duplicate nodes
        graph.delete(deletes)

        return graph, labels


class Application:
    """
    RAG application
    """

    def __init__(self):
        """
        Creates a new application.
        """

        # Load LLM
        self.llm = LLM(
            os.environ.get(
                "LLM",
                "TheBloke/Mistral-7B-OpenOrca-AWQ"
                if platform.machine() in ("x86_64", "AMD")
                else "TheBloke/Mistral-7B-OpenOrca-GGUF/mistral-7b-openorca.Q4_K_M.gguf",
            )
        )

        # Load embeddings
        self.embeddings = self.load()

        # Context size
        self.context = 10

        # Define prompt template
        template = """
Answer the following question using only the context below. Only include information
specifically discussed.

question: {question}
context: {context} """

        # Create RAG pipeline
        self.rag = RAG(
            self.embeddings,
            self.llm,
            system="You are a friendly assistant. You answer questions from users.",
            template=template,
            context=self.context,
        )

    def load(self):
        """
        Creates or loads an Embeddings instance.

        Returns:
            Embeddings
        """

        embeddings = None

        # Raw data path
        data = os.environ.get("DATA")

        # Embeddings database path
        database = os.environ.get("EMBEDDINGS", "neuml/txtai-wikipedia-slim")

        # Create a new embeddings database if:
        #  - A data path is provided OR
        #  - The database path is None
        if data or not database:
            # Create empty embeddings database
            embeddings = Embeddings(
                autoid="uuid5",
                path="intfloat/e5-large",
                instructions={"query": "query: ", "data": "passage: "},
                content=True,
                graph={"approximate": False, "minscore": 0.7},
            )

            # Index data directory, if provided
            if data:
                embeddings.index(self.stream(data))

                # Create LLM-generated topics
                self.infertopics(embeddings, 0)

        else:
            # Load existing model
            embeddings = Embeddings()
            if os.path.exists(database):
                embeddings.load(database)
            else:
                embeddings.load(provider="huggingface-hub", container=database)

        return embeddings

    def addurl(self, url):
        """
        Adds content at URL to this embeddings index.

        Args:
            url: input url
        """

        # Store number in index before indexing
        start = self.embeddings.count()

        # Add file to embeddings index
        self.embeddings.upsert(self.extract(url))

        # Create LLM-generated topics
        self.infertopics(self.embeddings, start)

    def stream(self, data):
        """
        Runs a textractor pipeline and streams extracted content from a data directory.

        Args:
            data: input data directory
        """

        # Stream sections from content
        for sections in self.extract(glob(f"{data}/**/*", recursive=True)):
            yield from sections

    def extract(self, inputs):
        """
        Extract sections from inputs using a Textractor pipeline.

        Args:
            inputs: input content

        Returns:
            extracted content
        """

        textractor = Textractor(paragraphs=True)
        return textractor(inputs)

    def infertopics(self, embeddings, start):
        """
        Traverses the graph associated with an embeddings instance and adds
        LLM-generated topics for each entry.

        Args:
            embeddings: embeddings database
            start: number of records before indexing
        """

        if embeddings.graph:
            batch = []
            for uid in tqdm(
                embeddings.graph.scan(),
                desc="Inferring topics",
                total=embeddings.graph.count() - start,
            ):
                # Infer topic if id is an autoid and topic is empty
                rid = embeddings.graph.attribute(uid, "id")
                topic = embeddings.graph.attribute(uid, "topic")
                if AutoId.valid(rid) and not topic:
                    text = embeddings.graph.attribute(uid, "text")
                    text = text if text else rid

                    batch.append((uid, text))
                    if len(batch) == 32:
                        self.topics(embeddings, batch)
                        batch = []

            if batch:
                self.topics(embeddings, batch)

    def topics(self, embeddings, batch):
        """
        Generates a batch of topics with a LLM. Topics are set directly on the embeddings
        instance.

        Args:
            embeddings: embeddings database
            batch: batch of (id, text) elements
        """

        prompt = """
Create a simple, concise topic for the following text. Only return the topic name.

Text:
{text}"""

        # Build batch of prompts
        prompts = []
        for uid, text in batch:
            text = text if re.search(r"\w+", text) else uid
            prompts.append([{"role": "user", "content": prompt.format(text=text)}])

        # Check if batch processing is enabled
        topicsbatch = os.environ.get("TOPICSBATCH")
        kwargs = {"batch_size": int(topicsbatch)} if topicsbatch else {}

        # Run prompt batch and set topics
        for x, topic in enumerate(self.llm(prompts, maxlength=2048, **kwargs)):
            # Set topic attribute
            uid = batch[x][0]
            embeddings.graph.addattribute(uid, "topic", topic)

            # Add topic to topics
            topics = embeddings.graph.topics
            if topics:
                if topic not in topics:
                    topics[topic] = []

                topics[topic].append(uid)

    def instructions(self):
        """
        Generates a welcome message with instructions.

        Returns:
            instructions
        """

        # Base instructions
        instructions = (
            "Ask a question such as `Who created Linux?`\n\n"
            f"{'**The index is currently empty**' if not self.embeddings.count() else ''}\n\n"
            "`📄 Data` can be added to this index as follows.\n\n"
            "- `# file path or URL`\n"
            "- `# custom notes and text as a string here!`"
        )

        # Graph instructions
        if "graph" in self.embeddings.config:
            instructions += (
                "\n\nThis index also supports `📈 GraphRAG`. Examples are shown below.\n"
                "- `gq: Tell me about Linux`\n"
                "  - Graph rag query, the `gq: ` prefix enables graph rag\n"
                "- `linux -> macos -> microsoft windows`\n"
                "  - Graph path query for a list of concepts separated by `->`\n"
                "  - The graph path is analyzed and described by the LLM\n"
                "- `linux -> macos -> microsoft windows gq: Tell me about Linux`\n"
                "  - Graph path with a graph rag query"
            )

        return instructions

    def run(self):
        """
        Runs a Streamlit application.
        """

        if "messages" not in st.session_state.keys():
            # Add instructions
            st.session_state.messages = [
                {"role": "assistant", "content": self.instructions()}
            ]

        if question := st.chat_input("Your question"):
            message = question
            if question.startswith("#"):
                message = f"Upload request for _{message.split('#')[-1].strip()}_"

            st.session_state.messages.append({"role": "user", "content": message})

        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.write(message["content"])

        if (
            st.session_state.messages
            and st.session_state.messages[-1]["role"] != "assistant"
        ):
            with st.chat_message("assistant"):
                logger.debug(f"USER INPUT: {question}")

                # Check for file upload
                if question.startswith("#"):
                    url = question.split("#")[1].strip()
                    with st.spinner(f"Adding {url} to index"):
                        self.addurl(url)

                    response = f"Added _{url}_ to index"
                    st.write(response)

                else:
                    # Check for Graph RAG
                    graph = GraphContext(self.embeddings, self.context)
                    question, context = graph(question)

                    # Graph RAG
                    if context:
                        logger.debug(
                            f"----------------- GRAPH CONTEXT ({len(context)})----------------"
                        )
                        for x in context:
                            logger.debug(x)

                        # Transform context into a list of text
                        context = [x["text"] for x in context]

                    # Vector RAG
                    else:
                        logger.debug("-----------------CONTEXT----------------")
                        for x in self.embeddings.search(question, self.context):
                            logger.debug(x)

                    # Run RAG
                    response = self.rag(question, context, maxlength=4096, stream=True)

                    # Render response
                    response = st.write_stream(response)

                st.session_state.messages.append(
                    {"role": "assistant", "content": response}
                )


@st.cache_resource(show_spinner="Initializing models and database...")
def create():
    """
    Creates and caches a Streamlit application.

    Returns:
        Application
    """

    return Application()


if __name__ == "__main__":
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    st.set_page_config(
        page_title="RAG with txtai",
        page_icon="🚀",
        layout="centered",
        initial_sidebar_state="auto",
        menu_items=None,
    )
    st.title(os.environ.get("TITLE", "🚀 RAG with txtai"))

    # Create and run RAG application
    app = create()
    app.run()
