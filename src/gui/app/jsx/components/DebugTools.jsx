// DEBUG TOOLS
// ===========
// A simple pane that acts as a companion to the development tools in your
// browser. Offers direct access to the middleware connection FreeNAS is using,
// as well as some debugging helpers.

"use strict";

var React = require("react");
var TWBS  = require("react-bootstrap");

// Middleware
var MiddlewareClient = require("../middleware/MiddlewareClient");
var MiddlewareStore  = require("../stores/MiddlewareStore");

// Tabs
var RPC = require("./DebugTools/RPC");


var DebugTools = React.createClass({

    getInitialState: function() {
      return {
          initialized : false
        , isVisible   : false
        , methods     : {}
      };
    }

  , handleMiddlewareChange: function( namespace ) {
      var newState = {};

      switch ( namespace ) {
        case "services":
          var availableServices = MiddlewareStore.getAvailableRPCServices();
          newState.services = availableServices;
          if ( availableServices.length ) {
            availableServices.forEach( function( service ) {
              MiddlewareClient.getMethods( service );
            });
          }
          break;

        case "methods":
          newState.methods = MiddlewareStore.getAvailableRPCMethods();
          break;
      }

      this.setState( newState );
    }

  , handleKeypress: function( event ) {
      if ( event.which === 192 && event.ctrlKey && event.shiftKey ) {

        if ( this.state.isVisible ) {
          MiddlewareStore.removeChangeListener( this.handleMiddlewareChange );
          this.setState({ isVisible: false });
        } else {
          MiddlewareStore.addChangeListener( this.handleMiddlewareChange );
          MiddlewareClient.getServices();
          this.setState({
              initialized : true
            , isVisible   : true
          });
        }

      }
    }

  , componentDidMount: function() {
      window.addEventListener("keyup", this.handleKeypress );
    }

  , componentWillUnmount: function() {
      window.removeEventListener("keyup", this.handleKeypress );
    }

  , render: function() {
    var content = null;

    if ( this.state.initialized ) {
      content = (
        <TWBS.TabbedArea className = "debug-nav">
          <TWBS.TabPane eventKey={1} tab="RPC">
            <RPC services={ this.state.services } methods={ this.state.methods } />
          </TWBS.TabPane>

          <TWBS.TabPane eventKey={2} tab="Events">

          </TWBS.TabPane>

          <TWBS.TabPane eventKey={3} tab="Tasks">

          </TWBS.TabPane>

          <TWBS.TabPane eventKey={4} tab="Stats">

          </TWBS.TabPane>

          <TWBS.TabPane eventKey={5} tab="Options">

          </TWBS.TabPane>

          <TWBS.TabPane eventKey={6} tab="Terminal">

          </TWBS.TabPane>
        </TWBS.TabbedArea>
      );
    }
    return (
      <div className = "debug-panel"
           style     = { this.state.isVisible ? {} : { display: "none" } }>
        { content }
      </div>
    );
  }

});

module.exports = DebugTools;
