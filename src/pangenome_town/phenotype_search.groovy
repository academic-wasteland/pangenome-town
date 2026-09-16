// Adapted from bio-ontology-research-group/indigena semantic_similarity.groovy.
@Grab(group='com.github.sharispe', module='slib-sml', version='0.9.1')
@Grab(group='net.sourceforge.owlapi', module='owlapi-api', version='4.2.5')
@Grab(group='net.sourceforge.owlapi', module='owlapi-apibinding', version='4.2.5')
@Grab(group='net.sourceforge.owlapi', module='owlapi-impl', version='4.2.5')
@Grab(group='ch.qos.logback', module='logback-classic', version='1.2.3')
@Grab(group='org.slf4j', module='slf4j-api', version='1.7.30')
@Grab(group='org.codehaus.gpars', module='gpars', version='1.1.0')
@Grab('me.tongfei:progressbar:0.9.3')


import org.semanticweb.owlapi.model.*
import  org.semanticweb.owlapi.apibinding.OWLManager

import slib.sml.sm.core.engine.SM_Engine
import slib.sml.sm.core.measures.Measure_Groupwise
import slib.sml.sm.core.metrics.ic.utils.*
import slib.sml.sm.core.utils.SMConstants
import slib.utils.ex.SLIB_Exception
import slib.sml.sm.core.utils.SMconf
import slib.graph.model.impl.graph.memory.GraphMemory
import slib.graph.io.conf.GDataConf
import slib.graph.io.util.GFormat
import slib.graph.io.loader.GraphLoaderGeneric
import slib.graph.model.impl.repo.URIFactoryMemory
import slib.graph.model.impl.graph.elements.*
import slib.sml.sm.core.metrics.ic.utils.*
import slib.graph.algo.utils.*


import org.openrdf.model.vocabulary.RDF

import groovyx.gpars.GParsPool

import java.util.HashSet

import groovy.cli.commons.CliBuilder
import java.nio.file.Paths


import org.slf4j.Logger
import org.slf4j.LoggerFactory

// Initialize the logger

import java.util.logging.Logger
import java.util.logging.Level
import java.util.logging.ConsoleHandler
import java.util.logging.SimpleFormatter

// Initialize the logger
Logger logger = Logger.getLogger(this.class.name)

// Configure the logger
ConsoleHandler handler = new ConsoleHandler()
handler.setLevel(Level.ALL)
handler.setFormatter(new SimpleFormatter())
logger.addHandler(handler)
logger.setLevel(Level.ALL)
logger.setUseParentHandlers(false)




import groovy.json.JsonSlurper
import groovy.json.JsonOutput
String rootDir = args[0]
def query = new JsonSlurper().parse(new File(args[1]))
String icMeasure = "resnik"
String pairwiseMeasure = query.measure ?: "lin"
String groupwiseMeasure = "bma"
def manager = OWLManager.createOWLOntologyManager()
def ontology = manager.loadOntologyFromOntologyDocument(new File(rootDir + "/upheno.owl"))

def classes = ontology.getClassesInSignature().collect { it.toStringID() }

def existingMpPhenotypes = new HashSet()
def existingHpPhenotypes = new HashSet()
classes.each { cls ->
    if (cls.contains("MP_")) {
        existingMpPhenotypes.add(cls)
    } else if (cls.contains("HP_")) {
        existingHpPhenotypes.add(cls)
    }
}

logger.info("Obtaining Gene-Phenotype associations from MGI_GenePheno.rpt. Genes are represented as MGI IDs and Phenotypes are represented as MP IDs")
def gene2pheno = new HashMap()

def mgiGenePhenoFile = new File(rootDir + "/MGI_GenePheno.rpt")
def mgiGenePheno = mgiGenePhenoFile.readLines()*.split('\t')

mgiGenePheno.each { line ->
    def genes = line[6].split("\\|")
    def phenotype = "http://purl.obolibrary.org/obo/" + line[4].replace(":", "_")
    
    if (phenotype in existingMpPhenotypes) {
	genes.each { gene ->
	    gene = "http://mowl.borg/" + gene.replace(":", "_")
	    if (!gene2pheno.containsKey(gene)) {
		gene2pheno[gene] = new HashSet()
	    }
	    gene2pheno[gene].add(phenotype)            
	}
    }
    
}

logger.info("gene2pheno size: ${gene2pheno.size()}")
logger.info("gene2pheno example: ${gene2pheno.take(1)}")

def geneDiseaseFile = new File(rootDir + "/gene_diseases.csv")
def geneDisease = geneDiseaseFile.readLines().tail()*.split(',')
def evalGenes = geneDisease.collect { it[0] }.unique().sort()


logger.info("Preparing Semantic Similarity Engine")
def factory = URIFactoryMemory.getSingleton()
def graphUri = factory.getURI("http://purl.obolibrary.org/obo/GDA_")
factory.loadNamespacePrefix("GDA", graphUri.toString())
def graph = new GraphMemory(graphUri)

def goConf = new GDataConf(GFormat.RDF_XML, Paths.get(rootDir, "upheno.owl").toString())
GraphLoaderGeneric.populate(goConf, graph)

def virtualRoot = factory.getURI("http://purl.obolibrary.org/obo/GDA_virtual_root")
def rooting = new GAction(GActionType.REROOTING)
rooting.addParameter("root_uri", virtualRoot.stringValue())
GraphActionExecutor.applyAction(factory, rooting, graph)


def withAnnotations = true

if (withAnnotations) {
    gene2pheno.each { gene, phenotypes ->
	phenotypes.each { phenotype ->
            def geneId = factory.getURI(gene)
	    def phenotypeId = factory.getURI(phenotype)
	    Edge e = new Edge(geneId, RDF.TYPE, phenotypeId)
	    graph.addE(e)
	}
    }
}

def engine = new SM_Engine(graph)

def icConf = null

if (withAnnotations) {
    icConf = new IC_Conf_Corpus(icMeasureResolver(icMeasure))
}
else {
    icConf = new IC_Conf_Topo(icMeasureResolver(icMeasure))
}
    

def smConfPairwise = new SMconf(pairwiseMeasureResolver(pairwiseMeasure))
smConfPairwise.setICconf(icConf)
def smConfGroupwise = new SMconf(groupwiseMeasureResolver(groupwiseMeasure))

def requested = query.phenotypes.collect { "http://purl.obolibrary.org/obo/" + it.replace(":", "_") }
def unknown = requested.findAll { !(it in classes) }
if (unknown) throw new IllegalArgumentException("Unknown phenotypes: " + unknown.join(", "))
def querySet = requested.collect { factory.getURI(it) }.toSet()
def scores = evalGenes.findAll { gene2pheno[it] }.collect { gene ->
    def annotated = gene2pheno[gene].collect { factory.getURI(it) }.toSet()
    def score = engine.compare(smConfGroupwise, smConfPairwise, annotated, querySet)
    [gene: gene.split("/").last().replace("_", ":"), score: score,
     phenotype_count: annotated.size()]
}.sort { a, b -> b.score <=> a.score ?: a.gene <=> b.gene }
println("RESULT_JSON=" + JsonOutput.toJson([ok:true, method:"INDIGENA semantic-similarity baseline: " + pairwiseMeasure + " / BMA",
    model:"SLIB 0.9.1; corpus Resnik IC normalized; UPheno 2025-07-21; MGI annotations",
    species:"Mus musculus", candidate_count:scores.size(), results:scores.take(query.limit as int),
    notice:"Mouse gene prioritization for research; not a trained INDIGENA KGE model or a clinical diagnosis."]))
static icMeasureResolver(measure) {
    if (measure.toLowerCase() == "sanchez") {
        return SMConstants.FLAG_ICI_SANCHEZ_2011
    } else if (measure.toLowerCase() == "resnik") {
	return SMConstants.FLAG_IC_ANNOT_RESNIK_1995_NORMALIZED

    } else {
        throw new IllegalArgumentException("Invalid IC measure: $measure")
    }
}

static pairwiseMeasureResolver(measure) {
    if (measure.toLowerCase() == "lin") {
        return SMConstants.FLAG_SIM_PAIRWISE_DAG_NODE_LIN_1998
    } else if (measure.toLowerCase() == "resnik") {
	return SMConstants.FLAG_SIM_PAIRWISE_DAG_NODE_RESNIK_1995
    } else {
        throw new IllegalArgumentException("Invalid pairwise measure: $measure")
    }
}

static groupwiseMeasureResolver(measure) {
    if (measure.toLowerCase() == "bma") {
        return SMConstants.FLAG_SIM_GROUPWISE_BMA
    } else if (measure.toLowerCase() == "bmm") {
	return SMConstants.FLAG_SIM_GROUPWISE_BMM
    } else {
        throw new IllegalArgumentException("Invalid groupwise measure: $measure")
    }
}

